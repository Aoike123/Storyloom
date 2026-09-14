import json
import shutil
import time

import pytest
from PIL import Image
from sqlalchemy import select

from backend.db import DATA, Record, Session, Task
from backend import worker
from backend.video_files import StorageError, digest, ffmpeg, media_path, probe_video
from backend.video_storage import (attach_artifact, create_use, export_manifest, generation_snapshot,
                                   register_artifact, snapshot_asset, use_entry, verify_file)


def clip_from(db, path, cid='storage_clip'):
    artifact=register_artifact(db,cid,path,{'origin':'test'})
    clip=Record(id=cid,kind='clip',data={'status':'pending','events':[],'annotated':False})
    attach_artifact(clip,artifact);db.add(clip);db.flush()
    return clip,artifact


def test_real_upload_is_independent_faststart_av_and_serves_ranges(client,sample_video):
    response=client.post('/api/upload',files={'file':('scene.mp4',sample_video.read_bytes(),'video/mp4')})
    assert response.status_code==200,response.text
    data=response.json()
    storage=client.get(f'/api/clips/{data["clip_id"]}/storage').json()
    artifact=storage['artifact']
    assert storage['schema_version']==1
    assert artifact['source']['sha256']==digest(sample_video)
    assert artifact['preparation']=='reused'
    meta=artifact['playback']['properties']
    assert meta['frame_count']==120 and meta['fps']=={'num':24,'den':1}
    assert meta['audio']=={'present':True,'codec':'aac','profile':'LC','sample_rate':48000,'channels':2}
    partial=client.get(data['media'],headers={'Range':'bytes=0-31'})
    assert partial.status_code==206 and partial.content==sample_video.read_bytes()[:32]
    with Session() as db:
        assert db.get(Record,data['clip_id']).data['status']=='pending'


def test_actual_trim_frames_and_old_selection_are_immutable(sample_video):
    with Session.begin() as db:
        clip,artifact=clip_from(db,sample_video)
        original=json.dumps(artifact.data,sort_keys=True)
        use=create_use(db,clip,1,4,{'shot_id':'S01','revision':2})
        assert use.data['range']=={'in_frame':24,'out_frame':96}
        assert use.data['end']-use.data['start']==3
        first=Image.open(media_path(use.data['boundaries']['first']['media'])).convert('RGB').getpixel((10,10))
        last=Image.open(media_path(use.data['boundaries']['last']['media'])).convert('RGB').getpixel((10,10))
        assert first[1]>220 and first[0]<30 and first[2]<30  # green at second 1
        assert min(last)>220  # white before second 4, NOT the black frame at second 4
        old=json.dumps(use.data,sort_keys=True)
        newer=create_use(db,clip,2,3,{'shot_id':'S01','revision':2})
        assert newer.id!=use.id
        assert json.dumps(use.data,sort_keys=True)==old
        assert json.dumps(artifact.data,sort_keys=True)==original
        assert digest(sample_video)==artifact.data['source']['sha256']
        assert create_use(db,clip,1,4,{'shot_id':'S01','revision':2}).id==use.id


def test_wrong_codec_and_fps_are_normalized_with_original_and_audio_preserved(sample_video):
    source=DATA/'media'/'incompatible.mkv'
    ffmpeg(['-v','error','-y','-i',sample_video,'-vf','fps=30','-c:v','mpeg4',
            '-c:a','pcm_s16le','-ar','44100','-ac','1',source])
    before=digest(source)
    with Session.begin() as db:
        clip,artifact=clip_from(db,source,'storage_transcode')
        assert artifact.data['source']['properties']['container']=='mkv'
        assert artifact.data['source']['sha256']==before
        assert artifact.data['preparation']=='transcoded'
        meta=artifact.data['playback']['properties']
        assert meta['video_codec']=='h264' and meta['frame_count']==120
        assert meta['fps']=={'num':24,'den':1} and meta['audio']['present']
        assert meta['audio']['sample_rate']==48000 and meta['audio']['channels']==2
    assert digest(source)==before


def test_asset_snapshot_survives_replacement_of_live_asset_file():
    path=DATA/'media'/'mutable_reference.png'
    Image.new('RGB',(256,256),'blue').save(path)
    with Session.begin() as db:
        asset=Record(id='storage_asset',kind='asset',version=2,data={'name':'角色','type':'character','description':'蓝衣','media':'/media/mutable_reference.png'})
        db.add(asset);db.flush()
        frozen=snapshot_asset(db,asset,2)
        captured=dict(frozen.data)
        asset.version=3;asset.data={**asset.data,'description':'红衣'}
        Image.new('RGB',(256,256),'red').save(path)
        later=snapshot_asset(db,asset,3)
        assert later.id!=frozen.id
        assert frozen.data==captured and frozen.data['description']=='蓝衣'
        assert verify_file(frozen.data['file'])!=path
        assert frozen.data['file']['sha256']!=later.data['file']['sha256']
        with pytest.raises(StorageError,match='版本已变化'):snapshot_asset(db,asset,2)


def test_legacy_registration_is_lazy_and_keeps_published_file_and_offsets(client,sample_video):
    path=DATA/'media'/'legacy_flat.mp4';shutil.copyfile(sample_video,path)
    with Session.begin() as db:
        db.add(Record(id='storage_legacy',kind='clip',data={'media':'/media/legacy_flat.mp4','duration':5,'locked':True,'reader_trim':{'start':1.1,'end':3.9}}))
    assert client.get('/api/clips/storage_legacy/storage').json()['schema_version']==0
    r=client.post('/api/clips/storage_legacy/register-storage')
    assert r.status_code==200,r.text
    assert r.json()['artifact']['source']['media']=='/media/legacy_flat.mp4'
    assert path.exists()
    with Session() as db:
        data=db.get(Record,'storage_legacy').data
        assert data['media']=='/media/legacy_flat.mp4' and data['reader_trim']=={'start':1.1,'end':3.9}


def test_file_tampering_prevents_publishing_a_selection(sample_video):
    with Session.begin() as db:
        clip,artifact=clip_from(db,sample_video,'storage_corrupt')
        use=create_use(db,clip,0,1,{'shot_id':'S01','revision':1})
        path=media_path(artifact.data['playback']['media'])
        path.write_bytes(path.read_bytes()+b'changed')
        with pytest.raises(StorageError,match='摘要'):use_entry(db,use,'occurrence_1')


def test_pending_or_invalid_file_is_not_registered(client):
    r=client.post('/api/upload',files={'file':('broken.mp4',b'not a video','video/mp4')})
    assert r.status_code==422
    with Session() as db:
        assert not list(db.scalars(select(Record).where(Record.kind=='clip_artifact')))
    with pytest.raises(StorageError):media_path('/media/../story.db')
    with pytest.raises(StorageError):media_path('https://example.com/video.mp4')


def test_downloaded_generation_recovers_without_resubmitting_or_polling(sample_video,monkeypatch):
    task_id='storage_recover'
    staging=DATA/'staging';staging.mkdir(exist_ok=True)
    shutil.copyfile(sample_video,staging/f'{task_id}.download')
    def forbidden(*args,**kwargs):pytest.fail('Recovery must use the completed local source')
    monkeypatch.setattr(worker,'submit_video',forbidden)
    monkeypatch.setattr(worker,'poll_video',forbidden)
    with Session.begin() as db:
        db.add(Task(id=task_id,kind='video',status='waiting',payload={'mode':'live','prompt':'角色转身','shot_id':'S01'},
                    result={'provider_id':'provider_123','submitted':time.time(),'download_complete':True,
                            'generation':{'provider':'test','model':'test-model'}}))
    assert worker.process_one('storage-worker')
    with Session.begin() as db:
        task=db.get(Task,task_id)
        assert task.status=='completed' and task.result['storage_schema_version']==1
        clip=db.get(Record,task.result['clip_id'])
        assert clip.data['status']=='pending' and clip.data['shot_id']=='S01'
        artifact=db.get(Record,task.result['artifact_id'])
        assert artifact.data['provenance']['provider_id']=='provider_123'
        # Simulate a crash after saving a result but before task completion.
        task.status='waiting';task.lease=0
    assert worker.process_one('storage-worker-2')
    with Session() as db:
        assert len(list(db.scalars(select(Record).where(Record.kind=='clip_artifact'))))==1
        assert db.get(Task,task_id).status=='completed'


def test_manifest_export_is_derived_and_repairable(client,sample_video):
    with Session.begin() as db:
        clip,artifact=clip_from(db,sample_video,'storage_manifest_clip')
        use=create_use(db,clip,1,4,{'shot_id':'S01','revision':1})
        release=Record(id='storage_release',kind='reader_release',version=1,
                       data={'schema_version':1,'entries':[use_entry(db,use,'occurrence_1')]})
        db.add(release);db.flush()
    path=export_manifest(release)
    data=json.loads(path.read_text(encoding='utf-8'))
    assert data['entries'][0]['use_id']==use.id
    assert data['entries'][0]['start']==1 and data['entries'][0]['end']==4
    path.unlink()
    assert export_manifest(release).exists()
    assert client.get('/api/reader/releases/storage_release/manifest').json()==data


def test_nonzero_timestamps_are_not_misreported_as_zero(sample_video):
    source=DATA/'media'/'offset.mp4'
    ffmpeg(['-v','error','-y','-i',sample_video,'-c','copy','-output_ts_offset','5','-movflags','+faststart',source])
    meta=probe_video(source)
    assert meta['first_pts']>0
    with Session.begin() as db:
        clip,artifact=clip_from(db,source,'storage_offset')
        assert artifact.data['playback']['properties']['first_pts']==0
        assert artifact.data['source']['sha256']==digest(source)


def test_variable_frame_rate_requires_a_normalized_playback_file(sample_video):
    source=DATA/'media'/'variable.mp4'
    ffmpeg(['-v','error','-y','-i',sample_video,'-an','-vf','select=lt(n\\,24)+gte(n\\,36)',
            '-fps_mode','passthrough','-c:v','libx264','-movflags','+faststart',source])
    assert probe_video(source)['constant_frame_rate'] is False
    with Session.begin() as db:
        clip,artifact=clip_from(db,source,'storage_variable')
        assert artifact.data['preparation']=='transcoded'
        assert artifact.data['playback']['properties']['fps']=={'num':24,'den':1}
        assert not artifact.data['playback']['properties']['audio']['present']


def test_submission_provenance_uses_the_exact_per_request_config(monkeypatch):
    from backend import providers
    config={'VIDEO_PROVIDER':'minimax','VIDEO_MODEL':'model-from-config','VIDEO_ENDPOINT':'https://video.example.test/v2/video_generation',
            'VIDEO_DURATION':'5','VIDEO_RESOLUTION':'480P','VIDEO_API_KEY':'test-secret-never-saved'}
    monkeypatch.setattr(providers,'model_config',lambda:dict(config))
    monkeypatch.setenv('VIDEO_MODEL','stale-process-value')
    def submit(*args,**kwargs):
        assert kwargs['config']==config
        return 'stored_provider_task'
    monkeypatch.setattr(worker,'submit_video',submit)
    monkeypatch.setattr(worker,'poll_video',lambda *args:('running',None))
    with Session.begin() as db:
        db.add(Task(id='storage_config',kind='video',payload={'mode':'live','prompt':'角色转身'}))
    worker.process_one('config-worker')
    with Session() as db:
        task=db.get(Task,'storage_config')
        assert task.result['generation']['model']=='model-from-config'
        assert 'test-secret-never-saved' not in json.dumps(task.result)
        assert task.status=='waiting'


def test_recovery_upgrades_a_clip_saved_by_the_old_worker(sample_video):
    task_id='storage_old_worker'
    with Session.begin() as db:
        db.add(Task(id=task_id,kind='video',status='running',owner='owner',payload={},result={'provider_id':'p123'}))
        db.add(Record(id=task_id+'_clip',kind='clip',data={'media':'/media/old_worker.mp4','duration':5,'status':'pending'}))
    worker.save_video_result(task_id,'owner',{'prompt':'已生成的镜头'},{'provider_id':'p123'},sample_video)
    with Session() as db:
        clip=db.get(Record,task_id+'_clip')
        assert clip.data['storage_schema_version']==1 and clip.data['artifact_id']
        assert db.get(Task,task_id).status=='completed'
