from backend.db import Session,Record,Task,DATA
from backend import production as p


def setup(db):
    from PIL import Image
    Image.new('RGB',(256,256),'blue').save(DATA/'media'/'test.png')
    db.add(Record(id='source_prod',kind='story_source',data={'title':'原作','author_name':'作者'}))
    db.add(Record(id='director_prod',kind='director',version=3,data={'source_id':'source_prod','status':'approved','treatment':{'premise':'一个脑洞'},
        'board':{'title':'短场景','shots':[{'id':'S01','motion_prompt':'单镜动作描述','generation_seconds':5,'edit_seconds':3}]}}))
    db.add(Record(id='image_asset',kind='asset',data={'status':'pending','media':'/media/test.png'}))
    db.add(Task(id='image_prod',kind='image',status='completed',payload={'director_id':'director_prod','director_version':3,'shot_id':'S01'},result={'asset_id':'image_asset'}))


def test_video_requires_review_and_repeated_click_reuses_task(client,monkeypatch):
    with Session.begin() as db:
        setup(db)
        db.add(Record(id='ref',kind='asset',data={'name':'人物参考','status':'pending','media':'/media/test.png'}))
        db.add(Record(id='visual_director_prod',kind='visual_config',data={'approved':True,'director_version':3,
            'bindings':{'S01':['ref']},'asset_versions':{'ref':1}}))
    monkeypatch.setattr(p,'settings',lambda:{'paid_enabled':True,'video_configured':True,'editable':{'VIDEO_PROVIDER':'minimax'}})
    body={'version':3,'confirm_paid':True}
    path='/api/production/director_prod/shots/S01/video'
    assert client.post(path,json=body).status_code==409
    with Session.begin() as db:db.get(Record,'ref').data={'name':'人物参考','status':'approved','media':'/media/test.png'}
    first=client.post(path,json=body);assert first.status_code==200
    assert client.post(path,json=body).json()['id']==first.json()['id']
    assert client.post(path,json={**body,'version':2}).status_code==409


def test_reference_change_replaces_the_previous_shot_video(client,monkeypatch):
    with Session.begin() as db:
        setup(db)
        db.add(Record(id='ref',kind='asset',data={'name':'人物参考','status':'approved','media':'/media/test.png'}))
        db.add(Record(id='visual_director_prod',kind='visual_config',data={'approved':True,'director_version':3,
            'bindings':{'S01':['ref']},'asset_versions':{'ref':2}}))
        db.add(Task(id='old-video',kind='video',status='completed',created=1,payload={
            'input_mode':'reference_images','director_id':'director_prod','director_version':3,
            'shot_id':'S01','reference_assets':{'ref':1}},result={'media':'/media/old.mp4'}))
    monkeypatch.setattr(p,'settings',lambda:{'paid_enabled':True,'video_configured':True,'editable':{'VIDEO_PROVIDER':'minimax'}})
    from backend.consistency import config as visual_config,stamp as visual_stamp
    with Session.begin() as db:
        db.get(Record,'ref').version=2
        token=visual_stamp(db,visual_config(db,'director_prod'))
        db.get(Task,'old-video').payload={**db.get(Task,'old-video').payload,'consistency_stamp':token}
    response=client.post('/api/production/director_prod/shots/S01/video',json={'version':3,'confirm_paid':True})
    assert response.status_code==200,response.text
    with Session() as db:assert db.get(Task,response.json()['id']).payload['revision_of']=='old-video'


def test_review_publish_reader_and_pause_intent(client,monkeypatch,sample_video):
    import shutil
    shutil.copyfile(sample_video,DATA/'media'/'prod.mp4')
    monkeypatch.setattr(p,'ready',lambda *a,**k:(None,'test',True))
    monkeypatch.setattr(p,'matching',lambda t,pid,v,sid=None,token=None:t.payload.get('director_id')==pid and t.payload.get('director_version')==v)
    monkeypatch.setattr(p,'validate_references',lambda *a:None)
    with Session.begin() as db:
        setup(db)
        db.add(Record(id='prod_clip',kind='clip',data={'duration':5,'media':'/media/prod.mp4','status':'pending'}))
        db.add(Task(id='video_prod',kind='video',status='completed',payload={'director_id':'director_prod','director_version':3,'shot_id':'S01'},result={'clip_id':'prod_clip'}))
    publish='/api/production/director_prod/publish'
    assert client.post(publish,json={'version':3,'confirm':True}).status_code==422
    path='/api/production/director_prod/shots/S01/approve-clip'
    assert client.post(path,json={'version':3,'start':1,'end':7,'confirm_visual':True}).status_code==422
    assert client.post(path,json={'version':3,'start':1,'end':4,'confirm_visual':True}).status_code==200
    result=client.post(publish,json={'version':3,'confirm':True});assert result.status_code==200
    release=result.json()
    assert client.post(publish,json={'version':3,'confirm':True}).json()['id']==release['id']
    stories=client.get('/api/reader/stories').json();assert len(stories)==1
    assert stories[0]['entries'][0]['start']==1
    assert client.post(path,json={'version':3,'start':0,'end':3,'confirm_visual':True}).status_code==409
    body={'release_id':release['id'],'index':0,'offset':2.35,'text':'换一个选择'}
    assert client.post('/api/reader/wishes',json=body).json()['status']=='saved'
    assert client.post('/api/reader/wishes',json={**body,'offset':8}).status_code==422


def test_old_version_outputs_do_not_enter_new_workspace(client):
    with Session.begin() as db:
        setup(db)
        db.get(Record,'director_prod').version=4
    shot=client.get('/api/production/director_prod').json()['shots'][0]
    assert shot['video_task'] is None and shot['clip'] is None
