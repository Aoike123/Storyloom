import httpx
import pytest
from backend import providers,production,director
from backend.db import Session,Record,Task,DATA
from test_production import setup
from test_provider_protocols import config


def test_h3_sends_multiple_reference_images_without_boundary_frame_roles(config,monkeypatch):
    sent=[]
    def post(url,**kwargs):
        sent.append(kwargs['json']);return httpx.Response(200,json={'task_id':'ref-task'})
    monkeypatch.setattr(providers.httpx,'post',post)
    images=['https://example.test/shot.png','https://example.test/costumed-person.png','https://example.test/room.png']
    assert providers.submit_video('人物在参考场景内行走','task',reference_images=images)=='ref-task'
    body=sent[0]
    assert [item['image_url']['url'] for item in body['content'][1:]]==images
    assert {item['role'] for item in body['content'][1:]}=={'reference_image'}
    assert body['ratio']=='16:9'


def test_invalid_reference_input_never_submits_or_reserves_budget(config,monkeypatch):
    monkeypatch.setattr(providers,'reserve_call',lambda *a,**k:pytest.fail('Invalid references must not be submitted'))
    for images in ([],['https://example.test/a.png']*10,['http://example.test/insecure.png']):
        with pytest.raises(providers.ProviderError):providers.submit_video('测试参考图片','task',reference_images=images)
    with pytest.raises(providers.ProviderError):providers.submit_video('测试参考图片','task','https://example.test/a.png',reference_images=['https://example.test/b.png'])


def test_video_freezes_reviewed_project_references(client,monkeypatch):
    with Session.begin() as db:
        setup(db)
        for aid,label,role in [('person-ref','方诺人物身份','character'),('costume-ref','方诺通勤装','costume'),('scene-ref','培训室','scene')]:
            db.add(Record(id=aid,kind='asset',data={'name':label,'status':'approved','type':role,'media':'/media/test.png'}))
        db.add(Record(id='prep_director_prod',kind='preproduction',data={'assets':{
            'person-ref':{'role':'character','name':'方诺'},'costume-ref':{'role':'costume','name':'通勤装'},
            'scene-ref':{'role':'scene','name':'培训室'}}}))
        db.add(Record(id='visual_director_prod',kind='visual_config',data={'approved':True,'director_version':3,
            'bindings':{'S01':['person-ref','costume-ref','scene-ref']},
            'asset_versions':{'person-ref':1,'costume-ref':1,'scene-ref':1}}))
    monkeypatch.setattr(production,'settings',lambda:{'paid_enabled':True,'video_configured':True,'editable':{'VIDEO_PROVIDER':'minimax'}})
    response=client.post('/api/production/director_prod/shots/S01/video',json={'version':3,'confirm_paid':True})
    assert response.status_code==200
    with Session() as db:
        payload=db.get(Task,response.json()['id']).payload
        assert payload['input_mode']=='reference_images' and 'frame_media' not in payload
        bindings=payload['input_snapshot']['asset_bindings']
        assert [item['asset_id'] for item in bindings]==['person-ref','costume-ref','scene-ref']
        assert [item['role'] for item in bindings]==['reference','reference','reference']
        assert len(payload['reference_media'])==3 and all('/snapshots/' in url for url in payload['reference_media'])
        assert payload['reference_assets']=={'person-ref':1,'costume-ref':1,'scene-ref':1}
        assert '参考图 1：方诺人物身份；用途：人物身份' in payload['prompt']
        assert '参考图 2：方诺通勤装；用途：独立服装' in payload['prompt']
        assert '参考图 3：培训室；用途：场景' in payload['prompt']
        assert '单镜动作描述' in payload['prompt']
        assert '不要把两栏画成两个人' not in payload['prompt'] and '拼接' not in payload['prompt']
        assert payload['prompt'].count('统一视觉：')==1
        assert '以所给首帧为起点' not in payload['prompt']


def test_new_schemas_write_reference_prompts_and_read_legacy_shot_data():
    from test_director import board
    parsed=director.Board.model_validate(board()).model_dump()
    assert all('reference_prompt' in shot and 'first_frame' not in shot for shot in parsed['shots'])
    assert 'reference_prompt' in director.Shot.model_json_schema()['properties']
    assert 'first_frame' not in director.ShotPrompt.model_json_schema()['properties']
