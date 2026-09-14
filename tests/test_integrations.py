import time
from backend.db import Session,Record,Task
from backend.domain import INITIAL,demo_plan
from backend.worker import process_one
from backend.providers import reserve_call,ProviderError
import pytest

def test_automatic_pipeline_queues_render_atomically(client):
    s=client.post('/api/sessions').json()
    r=client.post(f'/api/sessions/{s["id"]}/rewrite',json={'version':1,'index':1,'offset':2.35,'text':'林夏夺刀','auto_render':True}).json()
    process_one('w')
    with Session() as db:
        task=db.get(Task,'render_'+r['task']['id'])
        assert task and task.status=='queued' and task.revision==2
        assert db.get(Record,s['id']).data['active_task']==task.id

def test_paid_calls_no_longer_have_submission_limit(monkeypatch):
    import backend.providers as p
    monkeypatch.setattr(p,'settings',lambda:{'paid_enabled':True,'llm_configured':True,'paid_limit':0})
    first=reserve_call('llm','first')
    second=reserve_call('llm','second')
    assert first != second
    with Session() as db:
        assert db.get(Record,first).data['status']=='pending'
        assert db.get(Record,second).data['status']=='pending'


@pytest.mark.parametrize('attempts',[3,10,100])
def test_demo_task_resume_has_no_attempt_limit(client,attempts):
    with Session.begin() as db:
        db.add(Task(id='repeat-demo',kind='plan',status='failed',attempts=attempts,payload={'mode':'demo'}))
    response=client.post('/api/tasks/repeat-demo/resume',json={})
    assert response.status_code==200,response.text
    assert response.json()['status']=='queued' and response.json()['attempts']==attempts


def test_bridge_creates_video_jobs_without_external_calls(client):
    s=client.post('/api/sessions').json();plan=demo_plan('林夏夺刀',INITIAL)
    with Session.begin() as db:
        db.add(Task(id='bridge_test',kind='bridge',session_id=s['id'],revision=1,payload={'mode':'live','plan':plan.model_dump(),'state':INITIAL,'entries':s['entries'],'index':1,'offset':2}))
    assert process_one('w')
    with Session() as db:
        parent=db.get(Task,'bridge_test')
        assert parent.status=='waiting'
        assert len(parent.result['children'])==len(plan.beats)
        for child in parent.result['children']:assert db.get(Task,child).status=='queued'

def test_reviewed_bridge_can_commit_only_after_annotations(client):
    s=client.post('/api/sessions').json();plan=demo_plan('林夏夺刀',INITIAL);ids=[]
    with Session.begin() as db:
        for i,beat in enumerate(plan.beats):
            cid=f'live_clip_{i}';ids.append(cid)
            db.add(Record(id=cid,kind='clip',data={'title':beat.title,'duration':8,'status':'approved','annotated':False,
                'events':[],'expected_changes':beat.changes,'media':'/media/sample.mp4','demo':False}))
        db.add(Task(id='bridge_done',kind='bridge',status='needs_review',session_id=s['id'],revision=1,
                    payload={'mode':'live','plan':plan.model_dump(),'state':INITIAL,'entries':s['entries'],'index':1,'offset':2.35},result={'clip_ids':ids}))
    payload={'version':1,'task_id':'bridge_done','confirm_visual':True}
    assert client.post(f'/api/sessions/{s["id"]}/commit-bridge',json=payload).status_code==422
    for cid,beat in zip(ids,plan.beats):
        bad=client.post(f'/api/clips/{cid}/annotate',json={'events':[{'at':9,'text':beat.narration,'changes':beat.changes}]})
        assert bad.status_code==422
        annotation=client.post(f'/api/clips/{cid}/annotate',json={'events':[{'at':6.5,'text':beat.narration,'changes':beat.changes}]})
        assert annotation.status_code==200
        assert client.post('/api/review/'+cid,json={'status':'approved'}).status_code==200
    result=client.post(f'/api/sessions/{s["id"]}/commit-bridge',json=payload)
    assert result.status_code==200
    current=client.get(f'/api/sessions/{s["id"]}').json()
    assert current['version']==2 and current['entries'][1]['end']==2.35
    assert client.post('/api/review/'+ids[0],json={'status':'rejected'}).status_code==409

def test_annotated_changes_must_match_plan(client):
    with Session.begin() as db:db.add(Record(id='clip_custom',kind='clip',data={'duration':8,'expected_changes':{'knife':'heroine'}}))
    r=client.post('/api/clips/clip_custom/annotate',json={'events':[{'at':5,'text':'不符合计划','changes':{'knife':'attacker'}}]})
    assert r.status_code==422

def test_repeated_disarm_is_not_invented():
    state={**INITIAL,'knife':'secured'}
    assert demo_plan('女主夺下刀',state).status=='needs_review'

def test_cancelling_bridge_stops_unsubmitted_children(client):
    with Session.begin() as db:
        db.add(Task(id='parent',kind='bridge',status='waiting',result={'children':['child']}))
        db.add(Task(id='child',kind='video',status='queued',payload={'mode':'live'}))
    assert client.post('/api/tasks/parent/cancel').status_code==200
    with Session() as db:assert db.get(Task,'child').status=='cancelled'

def test_recovery_requires_known_provider_id(client):
    with Session.begin() as db:db.add(Task(id='lost',kind='video',status='needs_review',payload={'mode':'live'}))
    assert client.post('/api/tasks/lost/resume',json={}).status_code==422
    assert client.post('/api/tasks/lost/resume',json={'provider_id':'../../secret'}).status_code==422
    assert client.post('/api/tasks/lost/resume',json={'provider_id':'cgt-existing-task'}).status_code==200
    with Session() as db:
        task=db.get(Task,'lost')
        assert task.status=='queued' and task.result['provider_id']=='cgt-existing-task'
