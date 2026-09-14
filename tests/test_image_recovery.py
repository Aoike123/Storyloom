import copy
import pytest
from sqlalchemy import select
from backend import authors,worker
from backend.db import Session,Record,Task
from test_creative import creative,drain
from test_author_step_navigation import completed_work


def failed_image(client,status=451):
    completed_work(client,'preparing')
    with Session.begin() as db:
        run=db.get(Record,'creative_pid');old=copy.deepcopy(run.data)
        task=db.get(Task,run.data['items'][0]['task_id'])
        db.delete(db.get(Record,task.result['asset_id']))
        task.result={'generation_request':{'prompt':'已保存的无人静态设定图提示词','model':'original-model'}}
        task.status='needs_review';task.message=f'生图接口返回 HTTP {status}，未自动重新提交。'
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
