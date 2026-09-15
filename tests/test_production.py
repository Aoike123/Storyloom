from backend.db import Session,Record,Task,DATA
from backend import production as p
from backend.video_storage import attach_artifact,register_artifact


def saved_clip(db,cid,path='prod.mp4',**extra):
    """A clip as the worker saves it: the media file and its storage record together."""
    artifact=register_artifact(db,cid,DATA/'media'/path,{'origin':'generation','task_id':'video_'+cid})
    clip=Record(id=cid,kind='clip',data={'status':'pending',**extra})
    attach_artifact(clip,artifact);db.add(clip)
    return clip


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
    monkeypatch.setattr(p,'matching',lambda t,pid,v,sid=None,token=None:t.payload.get('director_id')==pid and t.payload.get('director_version')==v and (sid is None or t.payload.get('shot_id')==sid))
    monkeypatch.setattr(p,'validate_references',lambda *a:None)
    with Session.begin() as db:
        setup(db)
        saved_clip(db,'prod_clip')
        db.add(Task(id='video_prod',kind='video',status='completed',payload={'director_id':'director_prod','director_version':3,'shot_id':'S01'},result={'clip_id':'prod_clip'}))
    publish='/api/production/director_prod/publish'
    assert client.post(publish,json={'version':3,'confirm':True}).status_code==422
    path='/api/production/director_prod/shots/S01/approve-clip'
    assert client.post(path,json={'version':3,'start':1,'end':7,'confirm_visual':True}).status_code==422
    assert client.post(path,json={'version':3,'start':1,'end':4,'confirm_visual':True}).status_code==200
    result=client.post(publish,json={'version':3,'confirm':True});assert result.status_code==200
    release=result.json()
    again=client.post(publish,json={'version':3,'confirm':True});assert again.status_code==200,again.text
    assert again.json()['id']==release['id']
    # 重新发布是同一条发布记录的新修订，读清单快照因此不会互相覆盖。
    assert again.json()['manifest_revision']==release['manifest_revision']+1
    stories=client.get('/api/reader/stories').json();assert len(stories)==1
    assert stories[0]['entries'][0]['start']==1
    assert client.post(path,json={'version':3,'start':0,'end':3,'confirm_visual':True}).status_code==409
    body={'release_id':release['id'],'index':0,'offset':2.35,'text':'换一个选择'}
    assert client.post('/api/reader/wishes',json=body).json()['status']=='saved'
    assert client.post('/api/reader/wishes',json={**body,'offset':8}).status_code==422


def two_episode_setup(db):
    """Two episodes in the cut, one reviewed clip each, so publishing can stop in the middle."""
    setup(db)
    project=db.get(Record,'director_prod')
    project.data={**project.data,'segments':{'segments':[{'id':'G01'},{'id':'G02'}]},
        'board':{'title':'短场景','shots':[
            {'id':'G01-S01','segment_id':'G01','motion_prompt':'单镜动作描述','generation_seconds':5,'edit_seconds':3},
            {'id':'G02-S01','segment_id':'G02','motion_prompt':'单镜动作描述','generation_seconds':5,'edit_seconds':3}]}}
    for index,gid in enumerate(('G01','G02')):
        saved_clip(db,f'clip_{gid}')
        db.add(Task(id=f'video_{gid}',kind='video',status='completed',
            payload={'director_id':'director_prod','director_version':3,'shot_id':f'{gid}-S01'},
            result={'clip_id':f'clip_{gid}'}))


def test_publishing_an_unfinished_film_releases_the_episodes_that_are_ready(client,monkeypatch,sample_video):
    """读者能看到已完成的情节，制作中的情节不阻塞发布，也不被半截发布。"""
    import shutil
    shutil.copyfile(sample_video,DATA/'media'/'prod.mp4')
    path='/api/production/director_prod/shots/{}/approve-clip'
    monkeypatch.setattr(p,'ready',lambda *a,**k:(None,'test',True))
    monkeypatch.setattr(p,'matching',lambda t,pid,v,sid=None,token=None:t.payload.get('director_id')==pid and t.payload.get('director_version')==v and (sid is None or t.payload.get('shot_id')==sid))
    monkeypatch.setattr(p,'validate_references',lambda *a:None)
    with Session.begin() as db:two_episode_setup(db)
    # Only the first episode is reviewed.
    assert client.post(path.format('G01-S01'),json={'version':3,'start':1,'end':4,'confirm_visual':True}).status_code==200
    result=client.post('/api/production/director_prod/publish',json={'version':3,'confirm':True})
    assert result.status_code==200,result.text
    release=result.json()
    assert release['published_units']==['G01'] and release['total_units']==2
    assert release['units_complete'] is False and release['next_unit']=='G02'
    assert [entry['shot_id'] for entry in release['entries']]==['G01-S01']
    # The reader sees exactly the finished episode plus the progress on the card.
    story=client.get('/api/reader/catalog').json()['releases'][0]
    assert story['progress']=={'published_units':1,'total_units':2,'complete':False,'next_unit':'G02'}
    # Finishing the second episode and republishing replaces the release with the longer cut.
    assert client.post(path.format('G02-S01'),json={'version':3,'start':1,'end':4,'confirm_visual':True}).status_code==200
    longer=client.post('/api/production/director_prod/publish',json={'version':3,'confirm':True})
    assert longer.status_code==200,longer.text
    assert longer.json()['id']==release['id']
    assert longer.json()['published_units']==['G01','G02'] and longer.json()['units_complete'] is True
    assert len(longer.json()['entries'])==2
    assert len(client.get('/api/reader/catalog').json()['releases'])==1


def test_old_version_outputs_do_not_enter_new_workspace(client):
    with Session.begin() as db:
        setup(db)
        db.get(Record,'director_prod').version=4
    shot=client.get('/api/production/director_prod').json()['shots'][0]
    assert shot['video_task'] is None and shot['clip'] is None
