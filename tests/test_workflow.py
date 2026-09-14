import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from backend.db import Session,Record,Task
from backend.domain import INITIAL,JOIN_STATE,Plan,Beat,state_at,validate_plan,demo_plan
from backend.seed import CLIPS
from backend.worker import claim,process_one,run_task

def start(client):return client.post('/api/sessions').json()
def rewrite(client,s,text='林夏夺下歹徒的刀',index=1,offset=2):
    return client.post(f'/api/sessions/{s["id"]}/rewrite',json={'version':s['version'],'index':index,'offset':offset,'text':text,'mode':'demo'})

def test_pause_does_not_see_future():
    clips={c['id']:c for c in CLIPS}
    entries=[{'clip_id':c['id'],'start':0,'end':c['duration']} for c in CLIPS]
    state,events=state_at(entries,clips,1,5.9)
    assert state==INITIAL and events==[]
    state,_=state_at(entries,clips,1,6)
    assert state['help']=='called' and state['evidence']=='hidden'

def test_change_reaches_join_without_revoking_action():
    plan=demo_plan('林夏夺刀',INITIAL)
    assert plan.status=='ready'
    assert plan.beats[0].changes['knife']=='heroine'
    assert validate_plan(plan,INITIAL)==[]
    values=[b.changes.get('knife') for b in plan.beats]
    assert 'attacker' not in values

def test_conflict_is_not_silently_accepted():
    assert demo_plan('林夏销毁证据',INITIAL).status=='conflict'
    assert demo_plan('林夏开始跳舞',INITIAL).status=='needs_review'

def test_invalid_state_transition_rejected():
    p=Plan(status='ready',summary='bad',beats=[Beat(title='跳过取证',narration='直接提交',reason='无',changes=JOIN_STATE)])
    errors=validate_plan(p,INITIAL)
    assert any('尚未找到' in e for e in errors)

def test_optimistic_version_and_invalid_position(client):
    s=start(client)
    assert rewrite(client,s,index=50).status_code==422
    assert rewrite(client,s,offset=100).status_code==422
    assert rewrite(client,s).status_code==200
    assert rewrite(client,s).status_code==409

def test_stale_task_cannot_commit(client,monkeypatch):
    import backend.worker as worker
    s=start(client);r=rewrite(client,s).json();process_one('w1')
    t=client.post(f'/api/sessions/{s["id"]}/render',json={'plan_task_id':r['task']['id'],'version':r['session']['version']}).json()
    claimed=claim('w1');assert claimed==t['id']
    new=rewrite(client,r['session'],'林夏呼叫支援').json()
    monkeypatch.setattr(worker,'render_demo',lambda *args:'/media/test.mp4')
    run_task(claimed,'w1')
    current=client.get(f'/api/sessions/{s["id"]}').json()
    assert current['version']==new['session']['version']
    assert current['entries']==s['entries']

def test_full_branch_and_second_pause(client,monkeypatch):
    import backend.worker as worker
    monkeypatch.setattr(worker,'render_demo',lambda *args:'/media/test.mp4')
    s=start(client);r=rewrite(client,s).json();assert process_one('w')
    t=client.post(f'/api/sessions/{s["id"]}/render',json={'plan_task_id':r['task']['id'],'version':r['session']['version']})
    assert t.status_code==200;assert process_one('w')
    current=client.get(f'/api/sessions/{s["id"]}').json()
    assert current['version']==3
    assert current['entries'][1]['end']==2
    # Past the first generated event, the knife is held by the heroine.
    snapshot=client.get(f'/api/sessions/{s["id"]}/state?index=2&offset=7').json()
    assert snapshot['state']['knife']=='heroine'
    assert snapshot['state']['evidence']=='hidden'
    again=rewrite(client,current,'林夏呼叫支援',index=2,offset=7)
    assert again.status_code==200
    assert again.json()['task']['revision']==4

def test_only_one_worker_claims(client):
    s=start(client);r=rewrite(client,s).json()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,['a','b']))
    assert results.count(r['task']['id'])==1

def test_uncertain_paid_submission_not_retried():
    with Session.begin() as db:
        db.add(Task(id='uncertain',kind='video',status='running',payload={'mode':'live'},lease=time.time()-1))
    assert claim('new-worker') is None
    with Session() as db:assert db.get(Task,'uncertain').status=='needs_review'

def test_provider_id_survives_resume():
    with Session.begin() as db:
        db.add(Task(id='known',kind='video',status='waiting',payload={'mode':'live'},result={'provider_id':'abc'},lease=time.time()-1))
    assert claim('new-worker')=='known'
    with Session() as db:assert db.get(Task,'known').result['provider_id']=='abc'

def test_cancelled_render_cannot_commit(client,monkeypatch):
    import backend.worker as worker
    s=start(client);r=rewrite(client,s).json();process_one('w')
    t=client.post(f'/api/sessions/{s["id"]}/render',json={'plan_task_id':r['task']['id'],'version':2}).json()
    claim('w');client.post('/api/tasks/'+t['id']+'/cancel')
    monkeypatch.setattr(worker,'render_demo',lambda *args:'/media/test.mp4')
    run_task(t['id'],'w')
    current=client.get(f'/api/sessions/{s["id"]}').json()
    assert current['version']==2 and current['entries']==s['entries']

def test_asset_fork_leaves_original_unchanged(client):
    result=client.post('/api/assets/asset_lin/fork',json={'name':'林夏·副本','description':'改成蓝色风衣','type':'character'})
    assert result.status_code==200 and result.json()['parent_id']=='asset_lin'
    with Session() as db:assert '米白' in db.get(Record,'asset_lin').data['description']

def test_secret_is_not_returned(client):
    response=client.get('/api/settings').json()
    assert not any('key' in k.lower() for k in response)
    assert client.post('/api/video',json={'prompt':'一段测试动画','confirm_paid':False}).status_code==422

def test_cross_origin_mutation_rejected(client):
    assert client.post('/api/sessions',headers={'Origin':'https://evil.example'}).status_code==403
