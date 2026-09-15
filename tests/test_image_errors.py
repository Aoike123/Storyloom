import json
import httpx
import pytest
from sqlalchemy import select
from backend import image_provider,worker,provider_usage
from backend.db import Session,Record,Task,task_dict
from backend.image_errors import response_error,can_retry


@pytest.fixture
def image_call(monkeypatch):
    cfg={'IMAGE_PROVIDER':'siliconflow','IMAGE_MODEL':'test-image','IMAGE_API_KEY':'sk-private-image-key',
         'IMAGE_ENDPOINT':'https://api.siliconflow.cn/v1/images/generations'}
    monkeypatch.setattr(image_provider,'model_config',lambda:cfg)
    monkeypatch.setattr(image_provider,'reserve_call',lambda kind,tid:provider_usage.begin(kind,'test-image',tid))
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
    with Session() as db:
        public=task_dict(db.get(Task,'error-image'))
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


def test_a_failure_without_a_saved_response_is_reported_as_no_diagnosis():
    """没有保存供应商响应的失败只显示任务自己的说明，不推断原因，也不提供重试入口。"""
    for status in (402,451,503):
        with Session.begin() as db:
            task=Task(id='bare-'+str(status),kind='image',status='needs_review',
                      message=f'生图接口返回 HTTP {status}，未自动重新提交。',payload={},result={})
            db.add(task);db.flush()
            assert task_dict(task)['provider_error'] is None
            assert task.result=={} and can_retry(task) is False


def test_a_call_refused_before_submission_is_repairable_without_a_provider_response():
    """A picture stopped before submission has no response, and must still be generatable again."""
    from backend.image_errors import error_message,note_refusal
    reason='运营方提供的该模型 Key 今日已被供应商暂停。请填写自己的 API Key 继续。'
    with Session.begin() as db:
        db.add(Task(id='refused-image',kind='image',status='running',payload={'title':'公司急救培训室'},result={}))
        db.add(Task(id='refused-video',kind='video',status='running',payload={},result={}))
    note_refusal('refused-image',reason)
    note_refusal('refused-video',reason)
    with Session.begin() as db:
        image=db.get(Task,'refused-image');image.status='needs_review'
        error=task_dict(image)['provider_error']
        # Video diagnostics stay out of an image-only repair path.
        assert db.get(Task,'refused-video').result=={}
    assert error['source']=='not_submitted' and error['http_status'] is None
    assert error['provider_message']=='' and error['provider_code']=='' and error['request_id']==''
    assert error['advice']==reason
    # The interface shows the reason without inventing an HTTP status or a provider answer.
    assert error_message(error)==f'这次生图没有提交给供应商。{reason}'
    with Session() as db:assert can_retry(db.get(Task,'refused-image')) is True
    # A picture that already has its file is never generated again, refusal recorded or not.
    with Session.begin() as db:
        done=db.get(Task,'refused-image');done.status='completed';done.result={**done.result,'media':'/media/refused-image.png'}
    with Session() as db:assert can_retry(db.get(Task,'refused-image')) is False
