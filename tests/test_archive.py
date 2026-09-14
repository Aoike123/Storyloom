from backend.db import Session,Record,Task
from backend import director as d

def setup():
    with Session.begin() as db:
        db.add(Record(id='archive_source',kind='story_source',data={'labels':['脑洞'],'content_hash':'hash','content':'原文故事'}))
        db.add(Task(id='archive_task',kind='director',status='completed',payload={'project_id':'archive_project'}))
        db.add(Record(id='archive_project',kind='director',data={'source_id':'archive_source','task_id':'archive_task','status':'approved','brief':'旧方案'}))
        db.add(Record(id='old_image',kind='asset',data={'media':'/media/old.png','status':'approved'}))
        db.add(Task(id='old_frame',kind='image',status='completed',payload={'director_id':'archive_project'},result={'asset_id':'old_image'}))

def test_archive_history_and_restart(client,monkeypatch):
    setup();path='/api/director/projects/archive_project'
    r=client.post(path+'/archive',json={'version':1,'note':'画风不一致'})
    assert r.status_code==200 and r.json()['archived']
    assert client.get(path+'/history').json()['media'][0]['id']=='old_image'
    assert client.post(path+'/approve',json={'version':2,'confirm':True,'note':'不允许归档后审核'}).status_code==409
    monkeypatch.setattr(d,'settings',lambda:{'paid_enabled':True,'llm_configured':True})
    result=client.post('/api/director',json={'source_id':'archive_source','restart_of':'archive_project','brief':'新的画风设计','confirm_paid':True})
    assert result.status_code==200
    with Session() as db:
        new=db.get(Record,result.json()['project_id'])
        assert new.id!='archive_project' and new.data['restart_of']=='archive_project'
        assert new.data['status']=='generating' and 'board' not in new.data
        assert db.get(Record,'old_image') is not None
    assert client.post(path+'/archive',json={'version':1,'archived':False}).status_code==409
    assert client.post(path+'/archive',json={'version':2,'archived':False}).json()['archived'] is False

def test_archive_blocks_active_children(client):
    setup()
    with Session.begin() as db:db.add(Task(id='running_video',kind='video',status='waiting',payload={'director_id':'archive_project'}))
    assert client.post('/api/director/projects/archive_project/archive',json={'version':1}).status_code==409
    with Session() as db:assert not db.get(Record,'archive_project').data.get('archived')
