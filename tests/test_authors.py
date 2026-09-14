from fastapi.testclient import TestClient
from backend.app import app
from backend.db import Session,Record,Task,DATA
from backend import authors,creative as c,worker,director as d,preproduction as pp
from test_creative import creative,drain
from test_director import board
from test_catalog import story_api
from backend.production_nodes import KINDS as PRODUCTION_KINDS

def test_author_workspace_is_shared_without_sign_in(story_api):
    a=TestClient(app);b=TestClient(app)
    novel={'title':'演示小说','content':'我发现所有人每天都会失去一段记忆，只有我能记住昨天发生的一切。今天，我终于找到了原因。'}
    r=a.post('/api/author/stories/1/open',json={});assert r.status_code==200
    pid=r.json()['id']
    assert 'owner' not in r.json()
    assert a.get('/api/author/projects').json()==b.get('/api/author/projects').json()
    assert b.get('/api/author/projects/'+pid).json()['id']==pid
    assert b.get('/api/author/projects/missing').status_code==404
    # Legacy ownership metadata must not hide an existing work from demo visitors.
    with Session.begin() as db:
        row=db.get(Record,pid);row.data={**row.data,'owner':'previous-author'}
    assert b.get('/api/author/projects').json()[0]['id']==pid
    assert b.get('/api/author/projects/'+pid).status_code==200
    assert b.get('/api/settings').status_code==200
    (DATA/'media/unpublished.png').write_bytes(b'image')
    assert a.get('/media/unpublished.png').status_code==200
    assert b.get('/media/unpublished.png').status_code==200
    assert not a.cookies and not b.cookies


def test_author_flow_stops_only_for_assets_and_film(creative,monkeypatch,sample_video):
    import shutil
    client=creative
    assert client.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'温馨','confirm_paid':True}).status_code==200
    drain()
    a=TestClient(app)
    with Session.begin() as db:
        db.add(Record(id='work',kind='author_project',data={'director_id':'pid','stage':'preparing','art':'手绘漫画','tone':'温馨'}))
    assert authors.flow('unused',{'work_id':'work','phase':'preparing'})
    assert a.get('/api/author/projects/work').json()['stage']=='assets_review'
    assert a.post('/api/author/projects/work/generate',json={'confirm_paid':True}).status_code==422
    assert a.post('/api/author/projects/work/generate',json={'confirm':True,'confirm_paid':True}).status_code==200
    assert a.post('/api/author/projects/work/generate',json={'confirm':True,'confirm_paid':True}).status_code==409
    # A single-character shot stays as three separate identity, costume, and scene references.
    worker.process_one('author-test')
    pre=pp.get('pid')['config'];b=board()
    identity=next((aid for aid,spec in pre['assets'].items() if spec['role']=='character' and not spec.get('costume_asset_id')))
    costume=next((aid for aid,spec in pre['assets'].items() if spec['role']=='costume' and spec.get('identity_asset_id')==identity))
    scene=next((aid for aid,spec in pre['assets'].items() if spec['role']=='scene'))
    packed=[aid for aid,spec in pre['assets'].items() if spec['role']=='character' and spec.get('costume_asset_id')]
    assert packed==[]
    for shot in b['shots']:shot['assets']=[identity,costume,scene]
    answers=iter([b,{'shots':[{k:s[k] for k in ('id','first_frame','motion_prompt')} for s in b['shots']]},{'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'}])
    monkeypatch.setattr(d,'chat_json',lambda *args:(next(answers),{}))
    # Advance supervisor leases without sleeping or calling any paid provider.
    from sqlalchemy import select
    for _ in range(12):
        with Session.begin() as db:
            for t in db.scalars(select(Task).where(Task.kind.in_(['author_flow',*PRODUCTION_KINDS]),Task.status=='waiting')):t.lease=0
        if c.get_status('pid')=='videos_review':break
        worker.process_one('author-test')
        # Stop before executing the real video provider; image and LLM are mocked.
        for _ in range(20):
            with Session() as db:
                if list(db.scalars(select(Task).where(Task.kind=='video'))):break
            if not worker.process_one('author-test'):break
    assert c.get_status('pid')=='videos_review'
    with Session() as db:
        assert not [task for task in db.scalars(select(Task).where(Task.kind=='image')) if task.payload.get('preproduction_id')=='pid' or task.payload.get('asset_kind')=='dressed_character']
        assert not [asset for asset in db.scalars(select(Record).where(Record.kind=='asset'))
                    if asset.data.get('asset_kind')=='character_costume_reference']
    with Session.begin() as db:
        for t in db.scalars(select(Task).where(Task.kind=='video')):
            t.status='completed';t.result={'clip_id':t.id+'_clip','media':'/media/'+t.id+'.mp4'}
            shutil.copyfile(sample_video,DATA/'media'/f'{t.id}.mp4')
            db.add(Record(id=t.id+'_clip',kind='clip',data={'media':t.result['media'],'duration':5,'status':'pending'}))
    with Session.begin() as db:
        for t in db.scalars(select(Task).where(Task.kind.in_(PRODUCTION_KINDS),Task.status=='waiting')):t.lease=0
    assert worker.process_one('author-test')
    with Session() as db:
        nodes=list(db.scalars(select(Task).where(Task.kind.in_(PRODUCTION_KINDS))))
        assert len(nodes)==2 and all(node.status=='completed' for node in nodes)
    assert a.get('/api/author/projects/work').json()['stage']=='film_review'
    assert a.get('/api/reader/stories').json()==[]
    assert a.post('/api/author/projects/work/publish',json={'confirm':False}).status_code==422
    result=a.post('/api/author/projects/work/publish',json={'confirm':True})
    assert result.status_code==200,result.text
    assert len(a.get('/api/reader/stories').json())==1
    assert a.post('/api/author/projects/work/publish',json={'confirm':True}).json()['id']==result.json()['id']

def test_draft_does_not_schedule_paid_tasks(story_api):
    a=TestClient(app)
    assert a.post('/api/author/stories/1/open',json={}).status_code==200
    from sqlalchemy import select
    with Session() as db:assert not list(db.scalars(select(Task)))

def test_interrupted_supervisor_does_not_replay():
    with Session.begin() as db:
        db.add(Task(id='interrupted-author',kind='author_flow',status='running',lease=0,payload={'work_id':'unused'}))
    assert worker.claim('new-worker') is None
    with Session() as db:assert db.get(Task,'interrupted-author').status=='needs_review'
