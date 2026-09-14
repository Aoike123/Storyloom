import json
import httpx
import pytest
from sqlalchemy import select
from backend import image_provider,worker,billing
from backend.db import Session,Record,Task,task_dict
from backend.image_errors import response_error,can_retry


@pytest.fixture
def image_call(monkeypatch):
    cfg={'IMAGE_PROVIDER':'siliconflow','IMAGE_MODEL':'test-image','IMAGE_API_KEY':'sk-private-image-key',
         'IMAGE_ENDPOINT':'https://api.siliconflow.cn/v1/images/generations'}
    monkeypatch.setattr(image_provider,'model_config',lambda:cfg)
    monkeypatch.setattr(image_provider,'reserve_call',lambda kind,tid:billing.begin(kind,'test-image',tid))
    with Session.begin() as db:
        db.add(Task(id='error-image',kind='image',payload={'mode':'live','title':'公司急救培训室','prompt':'无人培训教室的场景设定图'}))
    return cfg


@pytest.mark.parametrize('status,category,summary',[(402,'balance','余额'),(451,'rejected','不能判定'),(429,'rate_limit','等待')])
def test_rejection_is_recorded_once_with_diagnostics_and_terminal_activity(image_call,monkeypatch,client,status,category,summary):
    calls=[]
    def post(*args,**kwargs):
        calls.append(kwargs['json'])
        return httpx.Response(status,json={'code':20001,'message':'Provider explanation'},headers={'x-siliconcloud-trace-id':'trace-image-123'})
    monkeypatch.setattr(image_provider.httpx,'post',post)
    assert worker.process_one('owner')
    assert not worker.process_one('owner')
    with Session() as db:
        task=db.get(Task,'error-image');error=task.result['provider_error']
        assert task.status=='needs_review' and error['http_status']==status and error['category']==category
        assert summary in error['advice']
        assert error['provider_message']=='Provider explanation' and error['provider_code']=='20001'
        assert error['request_id']=='trace-image-123'
        activity=task_dict(task)['activity']
        assert activity['phase']=='rejected' and activity['events'][-1]['at']==error['captured_at']
        assert db.scalar(select(Record).where(Record.kind=='usage')).data['status']=='rejected'
    assert len(calls)==1
    public=next(t for t in client.get('/api/tasks').json() if t['id']=='error-image')
    assert public['provider_error']==error


def test_only_safe_message_fields_are_persisted_and_exposed(image_call,monkeypatch,client):
    key=image_call['IMAGE_API_KEY']
    response=httpx.Response(451,json={'error':{'code':'policy_rejection','message':
        f'Request refused. Bearer {key}; api_key="other-credential"; https://example.test/image?signature=private-signature'},
        'request_body':{'Authorization':key,'debug_secret':'raw-body-secret'}},headers={'x-request-id':'trace-safe'})
    monkeypatch.setattr(image_provider.httpx,'post',lambda *a,**k:response)
    assert worker.process_one('owner')
    with Session() as db:
        text=json.dumps(task_dict(db.get(Task,'error-image')))
        for secret in (key,'other-credential','private-signature','raw-body-secret'):assert secret not in text
        assert 'Request refused' in text and 'trace-safe' in text


def test_non_json_errors_and_empty_messages_have_safe_fallbacks():
    error=response_error(httpx.Response(503,text='<html>private proxy details</html>',headers={'content-type':'text/html'}),{})
    assert error['category']=='service_error' and not error['provider_message']
    error=response_error(httpx.Response(451,text='Content not allowed',headers={'content-type':'text/plain'}),{})
    assert error['provider_message']=='Content not allowed'
    assert not response_error(httpx.Response(451,json={'error':{'message':{'unexpected':'shape'}}}),{})['provider_message']


def test_legacy_diagnostics_do_not_invent_response_or_retry_uncertain_results():
    for status in (402,451,503):
        with Session.begin() as db:
            task=Task(id='legacy-'+str(status),kind='image',status='needs_review',message=f'生图接口返回 HTTP {status}，未自动重新提交。',payload={},result={})
            db.add(task);db.flush()
            error=task_dict(task)['provider_error']
            assert error['source']=='legacy_status' and not error['provider_message'] and not error['request_id']
            assert task.result=={} and can_retry(task)==(status in (402,451))
