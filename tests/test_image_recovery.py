import copy
import httpx
import pytest
from sqlalchemy import select
from backend import authors,image_provider,worker
from backend.db import Session,Record,Task
from backend.model_access import create_account_session
from test_creative import creative,drain
from test_author_step_navigation import completed_work


def failed_image(client,status=402):
    """A picture the provider refused for a reason that does not involve the prompt text."""
    from backend.image_errors import error_message,response_error
    completed_work(client,'preparing')
    with Session.begin() as db:
        run=db.get(Record,'creative_pid');old=copy.deepcopy(run.data)
        task=db.get(Task,run.data['items'][0]['task_id'])
        db.delete(db.get(Record,task.result['asset_id']))
        error=response_error(httpx.Response(status,json={'code':20001,'message':'供应商说明'}),{})
        task.result={'generation_request':{'prompt':'已保存的无人静态设定图提示词','model':'original-model'},
                     'provider_error':error}
        task.status='needs_review';task.message=error_message(error)
        db.get(Task,'old-flow').status='needs_review'
        return task.id,old


def test_retry_only_failed_image_retains_design_and_completes_stage(creative):
    tid,old=failed_image(creative)
    with Session.begin() as db:
        db.add(Record(id='prep_pid',kind='preproduction',data={'versions':{},'assets':{}}))
    before=creative.get('/api/author/projects/back-work').json()
    assert [item['task_id'] for item in before['retryable_images']]==[tid]
    path='/api/author/projects/back-work/images/'+tid+'/retry'
    assert creative.post(path,json={}).status_code==422
    response=creative.post(path,json={'confirm_paid':True})
    assert response.status_code==200
    new_id=response.json()['task']['id']
    assert creative.post(path,json={'confirm_paid':True}).status_code==409
    with Session() as db:
        run=db.get(Record,'creative_pid')
        assert db.get(Record,'prep_pid').data.get('invalidated_at')
        assert run.data['items'][0]['task_id']==new_id
        assert run.data['items'][1:]==old['items'][1:]
        assert run.data['raw_design']==old['raw_design']
        assert db.get(Task,tid).status=='superseded' and db.get(Task,'old-flow').status=='superseded'
        assert db.get(Task,new_id).payload['prompt']=='已保存的无人静态设定图提示词'
        assert db.get(Task,new_id).payload['revision_of']==tid
    drain()
    result=creative.get('/api/author/projects/back-work').json()
    assert result['stage']=='assets_review' and not result['retryable_images']
    assert len(result['outputs'])==3 and all(job['status'] not in ('needs_review','failed') for job in result['jobs'])
    with Session() as db:assert len(list(db.scalars(select(Task).where(Task.kind=='image'))))==4


@pytest.mark.parametrize('state',['uncertain','completed','foreign','old_round','running'])
def test_invalid_retries_do_not_queue_generation(creative,state):
    tid,old=failed_image(creative,503 if state=='uncertain' else 451)
    with Session.begin() as db:
        if state=='completed':db.get(Task,tid).status='completed'
        if state=='foreign':db.get(Record,'back-work').data={**db.get(Record,'back-work').data,'director_id':'other'}
        if state=='old_round':db.get(Record,'pid').data={**db.get(Record,'pid').data,'archived':True,'superseded_by_run':'new-run'}
        if state=='running':db.add(Task(id='running-image',kind='image',status='running',payload={'creative_id':'pid'}))
    with Session() as db:count=len(list(db.scalars(select(Task))))
    assert creative.post('/api/author/projects/back-work/images/'+tid+'/retry',json={'confirm_paid':True}).status_code==409
    with Session() as db:assert len(list(db.scalars(select(Task))))==count


def test_resume_points_to_failed_image_without_repeating_failed_supervisor(creative):
    tid,_=failed_image(creative)
    with Session() as db:count=len(list(db.scalars(select(Task))))
    result=creative.post('/api/author/projects/back-work/resume',json={})
    assert result.status_code==409 and '先重试失败图片' in result.json()['detail']
    with Session() as db:assert len(list(db.scalars(select(Task))))==count


def own_key_access(client):
    token=client.post('/api/model-access/sessions',json={'mode':'own','keys':{'siliconflow':'sk-visitor-own-image-key'}}).json()['token']
    return {'X-Storyloom-Model-Access':token}


def stopped_by_the_operator_key(client,monkeypatch):
    """The provider refuses the operator's image key on the first picture, then the key is paused.

    The whole image path stays real — configuration, budget reservation and request — and only the
    image service answer is stubbed, so the pictures refused after the first one fail exactly the way
    a stopped run fails: inside the reservation, without ever reaching the provider.
    """
    completed_work(client,'preparing')
    account=create_account_session('visitor-login','uid-visitor')
    with Session.begin() as db:
        run=db.get(Record,'creative_pid');items=list(run.data['items'])
        for item in items:
            task=db.get(Task,item['task_id'])
            db.delete(db.get(Record,task.result['asset_id']))
            task.status='queued';task.lease=0;task.owner='';task.result={}
            task.session_id=account['access_id']
    monkeypatch.setenv('IMAGE_MODEL','test-image')
    monkeypatch.setenv('IMAGE_API_KEY','sk-operator-image-key')
    monkeypatch.setattr(worker,'generate_image',image_provider.generate_image)
    refused=[]
    def post(*args,**kwargs):
        refused.append(kwargs['headers']['Authorization'])
        return httpx.Response(402,json={'code':20001,'message':'余额不足'})
    monkeypatch.setattr(image_provider.httpx,'post',post)
    drain()
    return items,refused


def accepting_pictures(monkeypatch):
    """Every picture the image service accepts from now on, with the key it was sent with."""
    sent=[]
    def post(*args,**kwargs):
        sent.append(kwargs['headers']['Authorization'])
        return httpx.Response(200,json={'images':[{'url':'https://example.test/image'}]})
    monkeypatch.setattr(image_provider.httpx,'post',post)
    return sent


def test_own_key_finishes_every_picture_after_the_operator_key_ran_out(creative,monkeypatch):
    """Attaching an own key must unlock every picture of the stopped run, not just the first one.

    The provider refused the operator's key on the first picture, which pauses that key for the day.
    Every later picture was then stopped before submission and carries no provider response, so the
    repair panel used to list only the single picture that came back with an HTTP status.
    """
    items,refused=stopped_by_the_operator_key(creative,monkeypatch)
    assert refused==['Bearer sk-operator-image-key']  # Only the first picture reached the provider.
    with Session() as db:
        assert {db.get(Task,item['task_id']).status for item in items}=={'needs_review'}
    headers=own_key_access(creative)
    work=creative.get('/api/author/projects/back-work',headers=headers).json()
    assert [image['task_id'] for image in work['retryable_images']]==[item['task_id'] for item in items]
    # The generic "继续制作" now names every missing picture instead of only the first one.
    stopped=creative.post('/api/author/projects/back-work/resume',json={},headers=headers)
    assert stopped.status_code==409 and '先重试失败图片' in stopped.json()['detail']
    assert all(image['name'] in stopped.json()['detail'] for image in work['retryable_images'])
    sent=accepting_pictures(monkeypatch)
    for item in items:
        response=creative.post('/api/author/projects/back-work/images/'+item['task_id']+'/retry',
            json={'confirm_paid':True},headers=headers)
        assert response.status_code==200,response.json()
        drain()
    work=creative.get('/api/author/projects/back-work',headers=headers).json()
    # Every picture was generated again on the visitor's own key, not on the paused operator key.
    assert sent==['Bearer sk-visitor-own-image-key']*len(items)
    assert all(item['asset'] for item in work['creative']['items'])
    assert not work['retryable_images'] and work['stage']=='assets_review'


def test_repairing_several_pictures_keeps_every_next_button_usable(creative,monkeypatch):
    """A repair round stays open until the last rejected picture is back.

    The supervisor can only finish once every rejected picture is back. Queued with the first
    repair it failed on the pictures still missing, so the visitor saw the round closed after the
    first picture with "有任务需要处理" while the rest were still waiting to be retried.
    """
    items,_=stopped_by_the_operator_key(creative,monkeypatch)
    headers=own_key_access(creative)
    accepting_pictures(monkeypatch)
    def retry(item):
        return creative.post('/api/author/projects/back-work/images/'+item['task_id']+'/retry',
            json={'confirm_paid':True},headers=headers)
    for item in items[:-1]:
        assert retry(item).status_code==200,retry(item).json()
        drain()
        with Session() as db:
            # The round is still open: the supervisor waits for the last picture, not for this one.
            assert db.get(Record,'back-work').data['supervisor']=='old-flow'
            assert not [task for task in db.scalars(select(Task)) if task.kind=='author_flow' and task.status in ('queued','running','waiting')]
    assert retry(items[-1]).status_code==200
    with Session() as db:
        supervisor=db.get(Record,'back-work').data['supervisor']
        assert supervisor!='old-flow' and db.get(Task,supervisor).status in ('queued','running','waiting')
    drain()
    work=creative.get('/api/author/projects/back-work',headers=headers).json()
    assert work['stage']=='assets_review' and not work['retryable_images']
    assert all(item['asset'] for item in work['creative']['items'])


# The prompt the provider refused: it asks for the bare natural body of a human torso and limbs.
REJECTED_PROMPT='身份 C004：完整展示自然体表与身体结构，人类躯干与四肢无体毛，不穿人类服装。'
EDITED_PROMPT='身份 C004：神话生物或非人角色身份三视图设定板，该角色穿着无标识、低遮挡的中性基础短装，完整着装，不使用裸露造型。'


def refused_for_content(tid,prompt=REJECTED_PROMPT):
    """Mark one failed picture as a content-policy (HTTP 451) rejection with a saved prompt."""
    from backend.image_errors import error_message,response_error
    error=response_error(httpx.Response(451,json={'code':20002,
        'message':'It appears to contain prohibited or sensitive content. Please adjust your input and try again.'}),{})
    with Session.begin() as db:
        task=db.get(Task,tid)
        task.result={**task.result,'provider_error':error,
            'generation_request':{'prompt':prompt,'model':'test-image'}}
        task.status='needs_review';task.message=error_message(error)
    return error


def content_policy_image(client,monkeypatch):
    """One picture the provider refused for its content, ready for a rewritten prompt."""
    completed_work(client,'preparing')
    monkeypatch.setenv('IMAGE_MODEL','test-image')
    monkeypatch.setenv('IMAGE_API_KEY','sk-operator-image-key')
    monkeypatch.setattr(worker,'generate_image',image_provider.generate_image)
    with Session.begin() as db:
        run=db.get(Record,'creative_pid')
        task=db.get(Task,run.data['items'][0]['task_id'])
        db.delete(db.get(Record,task.result['asset_id']))
        task.result={}  # The provider returned no picture, so no asset id is left behind.
        db.get(Task,'old-flow').status='needs_review'
    refused_for_content(run.data['items'][0]['task_id'])
    return run.data['items'][0]['task_id']


def test_a_content_policy_rejection_is_repaired_by_rewriting_the_prompt(creative,monkeypatch):
    """HTTP 451 repeats for the same text, so the panel hands the prompt back to the author."""
    tid=content_policy_image(creative,monkeypatch)
    headers=own_key_access(creative)
    image=creative.get('/api/author/projects/back-work',headers=headers).json()['retryable_images'][0]
    assert image['task_id']==tid and image['needs_prompt_edit'] is True
    assert image['category']=='content_policy' and image['prompt']==REJECTED_PROMPT
    sent=accepting_pictures(monkeypatch)
    response=creative.post('/api/author/projects/back-work/images/'+tid+'/retry',
        json={'confirm_paid':True,'prompt':EDITED_PROMPT},headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['task']['id']!=tid
    drain()
    work=creative.get('/api/author/projects/back-work',headers=headers).json()
    assert not work['retryable_images'] and work['stage']=='assets_review'
    # The rewritten text is what reached the provider, on the visitor's own key.
    assert sent==['Bearer sk-visitor-own-image-key']
    with Session() as db:
        new=db.get(Task,work['creative']['items'][0]['task_id'])
        assert new.payload['prompt']==EDITED_PROMPT
        assert new.payload['prompt_source']=='manual_edit' and new.payload['prompt_edited'] is True
        assert new.payload['revision_of']==tid
        assert db.get(Task,tid).status=='superseded'
        audit=[row for row in db.scalars(select(Record).where(Record.kind=='audit'))
               if row.data.get('action')=='image_prompt_edited']
        assert audit and audit[0].data['prompt']==EDITED_PROMPT[:1500]


def test_an_edited_prompt_is_refused_for_failures_that_do_not_need_one(creative,monkeypatch):
    """A quota or rate-limit failure keeps its approved prompt; editing it would change the design."""
    tid,_=failed_image(creative)
    headers=own_key_access(creative)
    image=creative.get('/api/author/projects/back-work',headers=headers).json()['retryable_images'][0]
    assert image['needs_prompt_edit'] is False and image['prompt']==''
    path='/api/author/projects/back-work/images/'+tid+'/retry'
    response=creative.post(path,json={'confirm_paid':True,'prompt':'换一段完全不同的提示词，长度足够通过校验'},headers=headers)
    assert response.status_code==409 and '不需要改写提示词' in response.json()['detail']
    assert creative.post(path,json={'confirm_paid':True},headers=headers).status_code==200


def test_the_batch_repair_leaves_content_rejections_to_their_own_editor(creative,monkeypatch):
    """Resubmitting a rejected prompt would only be refused again, so 全部重试 skips it."""
    items,_=stopped_by_the_operator_key(creative,monkeypatch)
    headers=own_key_access(creative)
    refused_for_content(items[1]['task_id'])
    work=creative.get('/api/author/projects/back-work',headers=headers).json()
    assert [image['task_id'] for image in work['retryable_images']]==[item['task_id'] for item in items]
    assert [image['needs_prompt_edit'] for image in work['retryable_images']]==[False,True,False]
    accepting_pictures(monkeypatch)
    response=creative.post('/api/author/projects/back-work/images/retry',json={'confirm_paid':True},headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['needs_prompt_edit']==[items[1]['task_id']]
    # The rejected picture keeps its own task until its prompt is rewritten.
    with Session() as db:
        assert db.get(Task,items[1]['task_id']).status=='needs_review'
        queued=[task for task in db.scalars(select(Task).where(Task.kind=='image',Task.status=='queued'))]
        assert {task.payload['revision_of'] for task in queued}=={items[0]['task_id'],items[2]['task_id']}
