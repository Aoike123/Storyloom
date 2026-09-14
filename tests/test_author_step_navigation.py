import copy
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from backend import authors,worker,director,asset_workflow
from backend.db import Record,Session,Task,DATA
from test_creative import creative,drain


def completed_work(client,stage='assets_review'):
    client.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'温馨','confirm_paid':True});drain()
    with Session.begin() as db:
        run=db.get(Record,'creative_pid')
        original=copy.deepcopy(run.data)
        db.add(Task(id='old-flow',kind='author_flow',status='completed',payload={'work_id':'back-work','phase':'preparing'}))
        db.add(Record(id='back-work',kind='author_project',data={'title':'回退测试','source_id':'source','stage':stage,
            'director_id':'pid','supervisor':'old-flow','art':'手绘漫画','tone':'温馨','assets_confirmed_at':1}))
    return original


def restart(client,**changes):
    return client.post('/api/author/projects/back-work/start',json={
        'art':'清洁墨线，低饱和冷色板','tone':'轻喜剧','confirm_paid':True,'restart':True,'restart_from':'style',**changes})


def test_retest_invalidates_downstream_preserves_history_and_uses_new_director(creative):
    old=completed_work(creative)
    assert restart(creative).status_code==200
    assert restart(creative).status_code==409  # Cannot start overlapping test rounds.
    with Session() as db:
        work=db.get(Record,'back-work')
        assert work.data['stage']=='preparing'
        assert 'director_id' not in work.data and 'assets_confirmed_at' not in work.data
        assert work.data['attempt_history'][0]['director_id']=='pid'
        assert work.data['attempt_history'][0]['superseded_by_run']==work.data['run_id']
        previous=db.get(Record,'pid')
        assert previous.data['archived'] and previous.data['status']=='superseded'
        for item in old['items']:
            task=db.get(Task,item['task_id'])
            assert task.status=='completed' and db.get(Record,task.result['asset_id']) is not None
        assert db.get(Record,'creative_pid').data==old
    assert worker.process_one('new-round')
    with Session() as db:
        work=db.get(Record,'back-work');new_id=work.data['director_id']
        assert new_id!='pid'
        new=db.get(Record,new_id)
        assert new.data['request_key']=='back-work:'+work.data['run_id']
        assert '低饱和冷色板' in new.data['brief']
        assert len(list(db.scalars(select(Record).where(Record.kind=='director'))))==2
        supervisor=db.get(Task,work.data['supervisor']);payload=dict(supervisor.payload)
    # Recover a stop after director creation but before linking it to the author work.
    with Session.begin() as db:
        work=db.get(Record,'back-work');work.data={k:v for k,v in work.data.items() if k!='director_id'}
    assert authors.flow(supervisor.id,payload) is False
    with Session() as db:
        assert db.get(Record,'back-work').data['director_id']==new_id
        assert len(list(db.scalars(select(Record).where(Record.kind=='director'))))==2


def test_browsing_previous_step_does_not_mutate_or_create_tasks(creative):
    completed_work(creative)
    before=creative.get('/api/author/projects/back-work').json()
    with Session() as db:count=len(list(db.scalars(select(Task))))
    after=creative.get('/api/author/projects/back-work?step=style').json()
    assert after['stage']==before['stage']=='assets_review'
    assert after['director_id']==before['director_id'] and after['version']==before['version']
    with Session() as db:assert len(list(db.scalars(select(Task))))==count


def test_retest_does_not_interrupt_active_jobs_or_skip_explicit_submission(creative):
    completed_work(creative)
    assert restart(creative,confirm_paid=False).status_code==422
    assert restart(creative,restart=False).status_code==409
    with Session.begin() as db:
        db.add(Task(id='still-running',kind='image',status='running',payload={'creative_id':'pid'}))
    assert restart(creative).status_code==409
    with Session() as db:
        assert db.get(Task,'still-running').status=='running'
        assert not db.get(Record,'pid').data.get('archived')
        assert db.get(Record,'back-work').data['director_id']=='pid'


def test_old_scheduler_and_assets_cannot_update_or_feed_new_round(creative):
    old=completed_work(creative)
    assert restart(creative).status_code==200
    with pytest.raises(HTTPException,match='旧轮次'):
        authors.flow('old-flow',{'work_id':'back-work','phase':'preparing'})
    with Session.begin() as db:
        db.get(Task,'old-flow').status='needs_review'
        task=db.get(Task,old['items'][0]['task_id']);task.status='failed'
    assert creative.post('/api/tasks/old-flow/resume',json={}).status_code==404
    assert creative.post('/api/tasks/'+task.id+'/resume',json={}).status_code==404
    with Session() as db:
        asset=db.get(Record,task.result['asset_id'])
        with pytest.raises(HTTPException,match='旧轮次'):asset_workflow.validate_asset_origin(db,asset)
        assert db.get(Record,'back-work').data['stage']=='preparing'


def test_retest_invalidates_published_version_without_deleting_media(creative):
    completed_work(creative,'published')
    media=DATA/'media'/'old-release.mp4';media.write_bytes(b'historical-video')
    with Session.begin() as db:
        db.add(Record(id='old-release',kind='reader_release',data={'director_id':'pid','source_id':'source',
            'title':'旧版','source_title':'原作','entries':[{'media':'/media/old-release.mp4','start':0,'end':1}]}))
        work=db.get(Record,'back-work');work.data={**work.data,'release_id':'old-release'}
    assert len(creative.get('/api/reader/stories').json())==1
    assert restart(creative).status_code==200
    assert creative.get('/api/reader/stories').json()==[]
    with Session() as db:
        work=db.get(Record,'back-work');release=db.get(Record,'old-release');project=db.get(Record,'pid')
        assert release.data['status']=='superseded' and release.data['superseded_by_run']==work.data['run_id']
        assert 'release_id' not in work.data
        assert work.data['attempt_history'][0]['release_id']=='old-release'
        version=project.version
    assert media.read_bytes()==b'historical-video'
    assert creative.post('/api/director/projects/pid/archive',json={'version':version,'archived':False}).status_code==409
    assert creative.post('/api/reader/wishes',json={'release_id':'old-release','index':0,'offset':0,'text':'新的想法'}).status_code==409
