import pytest
from fastapi import HTTPException
from PIL import Image
from backend.db import Session,Record,Task,DATA
from backend import preproduction as p

def prepare(client):
    Image.new('RGB',(256,256)).save(DATA/'media'/'stage.png')
    with Session.begin() as db:
        db.add(Record(id='prep_test',kind='director',data={'status':'awaiting_preproduction'}))
        for aid in ['actor','scene','prop']:db.add(Record(id=aid,kind='asset',data={'status':'approved','media':'/media/stage.png'}))
    config={'style':'Consistent comic style and illumination','assets':{a:{'name':a,'role':r,'notes':'Fixed look and spatial position'} for a,r in [('actor','character'),('scene','scene'),('prop','prop')]}}
    assert client.post('/api/preproduction/prep_test',json=config).status_code==200
    return client.get('/api/preproduction/prep_test').json()['stamp']

def test_trial_requires_both_cast_and_set(client,monkeypatch):
    stamp=prepare(client)
    monkeypatch.setattr(p,'settings',lambda:{'paid_enabled':True,'image_configured':True})
    body={'stamp':stamp,'assets':['actor','prop'],'prompt':'Full costume trial','confirm_paid':True}
    assert client.post('/api/preproduction/prep_test/trial',json=body).status_code==422
    r=client.post('/api/preproduction/prep_test/trial',json={**body,'assets':['actor','scene','prop']})
    assert r.status_code==200
    with Session() as db:
        t=db.get(Task,r.json()['id'])
        assert len(t.payload['reference_media'])==3 and t.payload['preproduction_stamp']==stamp

def test_trial_coverage_and_stale_assets(client):
    stamp=prepare(client)
    with Session.begin() as db:
        db.add(Task(id='trial',kind='image',status='completed',payload={'preproduction_id':'prep_test','preproduction_stamp':stamp,'reference_ids':['actor','scene']},result={'asset_id':'trial_image'}))
        db.add(Record(id='trial_image',kind='asset',data={'status':'approved','source_task':'trial'}))
    body={'stamp':stamp,'asset_ids':['trial_image'],'note':'Review all costume and spatial details','confirm':True}
    assert client.post('/api/preproduction/prep_test/approve',json=body).status_code==422
    with Session.begin() as db:
        t=db.get(Task,'trial');t.payload={**t.payload,'reference_ids':['actor','scene','prop']}
    assert client.post('/api/preproduction/prep_test/approve',json=body).status_code==200
    with Session() as db:assert p.ready(db,'prep_test')['stamp']==stamp
    with Session.begin() as db:db.get(Record,'actor').version+=1
    with Session() as db:
        with pytest.raises(HTTPException):p.ready(db,'prep_test')

def test_board_rejects_unselected_assets():
    from types import SimpleNamespace
    board=SimpleNamespace(shots=[SimpleNamespace(id='S01',assets=['invented','scene'])])
    with pytest.raises(HTTPException,match='未选定素材 ID：invented'):
        p.validate_board(board,{'assets':{'scene':{'role':'scene'}}})


def test_storyboard_contract_exposes_copy_safe_identity_costume_sets():
    from test_director import reference_prep
    contract=p.storyboard_asset_contract(reference_prep())
    assert contract['max_semantic_assets_per_shot']==12
    assert contract['max_reference_images_per_shot']==9
    assert contract['character_reference_sets']==[
        {'character':'女主','costume':'日常定装','asset_ids':['actor','costume']}]
    assert {scene['asset_id'] for scene in contract['scene_references']}=={'restroom','apartment'}


def test_board_reports_reference_limit_instead_of_claiming_assets_are_unselected():
    from types import SimpleNamespace
    from test_director import reference_prep
    prep=reference_prep()
    crowd={f'extra{i}':{'role':'character','name':f'同事{i}','identity_asset_id':f'extra{i}'} for i in range(8)}
    prep['assets'].update(crowd)
    board=SimpleNamespace(shots=[SimpleNamespace(
        id='S03',assets=['actor','costume',*crowd,'restroom'])])
    with pytest.raises(HTTPException,match='S03 的 assets 完整绑定为 11 张.*最多接收 9 张'):
        p.validate_board(board,prep)


def test_board_expands_legacy_stitched_boards_back_to_project_assets():
    from backend.director import Board
    from test_director import board,reference_prep
    prep=reference_prep();prep['assets'].update({
        'other':{'role':'character','name':'跟踪者','identity_asset_id':'other','requires_costume':True},
        'other-costume':{'role':'costume','name':'灰夹克','identity_asset_id':'other'},
        'actor-packed':{'role':'character','name':'女主 · 日常定装拼接参考','identity_asset_id':'actor','costume_asset_id':'costume'},
        'other-packed':{'role':'character','name':'跟踪者 · 灰夹克拼接参考','identity_asset_id':'other','costume_asset_id':'other-costume'},
    })
    raw=board()
    for shot in raw['shots']:shot['assets']=['actor','costume','restroom']
    raw['shots'][0]['assets']=['actor-packed','other-packed','restroom']
    repaired,changes=p.repair_board_assets(Board.model_validate(raw),prep)
    assert repaired.shots[0].assets==['actor','costume','other','other-costume','restroom']
    assert repaired.shots[1].assets==['actor','costume','restroom']
    assert [change['action'] for change in changes]==[
        'expand_identity_costume_reference','expand_identity_costume_reference']
    p.validate_board(repaired,prep)


def test_board_repairs_visible_named_character_and_costume_as_separate_references():
    from backend.director import Board
    from test_director import board,reference_prep
    prep=reference_prep();prep['assets'].update({
        'grey-man':{'role':'character','name':'灰夹克男人','identity_asset_id':'grey-man','requires_costume':True},
        'grey-coat':{'role':'costume','name':'灰夹克男人 跟踪定装','identity_asset_id':'grey-man'},
    })
    raw=board()
    for item in raw['shots']:item['assets']=['actor','costume','restroom']
    shot=raw['shots'][0]
    shot['reference_prompt']='女主望向巷子深处，灰夹克仅呈现极小块轮廓。'
    shot['assets']=['actor','costume','restroom']
    repaired,changes=p.repair_board_assets(Board.model_validate(raw),prep)
    assert repaired.shots[0].assets==['actor','costume','restroom','grey-man','grey-coat']
    assert {change['action'] for change in changes if change['shot_id']=='S01'}=={
        'bind_named_character','bind_unique_character_costume'}
    p.validate_board(repaired,prep)


def test_over_limit_binding_is_stitched_by_the_local_image_tool():
    from backend.director import Board
    from test_director import board,reference_prep
    prep=reference_prep()
    crowd={f'extra{i}':{'role':'character','name':f'同事{i}','identity_asset_id':f'extra{i}'} for i in range(7)}
    prep['assets'].update(crowd)
    prep['assets']['actor-packed']={'role':'character','name':'女主 · 日常定装拼接参考',
        'identity_asset_id':'actor','costume_asset_id':'costume'}
    plan=p.plan_board_asset_packing(type('Board',(),{'shots':[type('Shot',(),{
        'id':'S01','assets':['actor','costume',*crowd,'restroom']})()]})(),prep)
    assert plan=={'S01':[('actor','costume')]}
    raw=board()
    for shot in raw['shots']:shot['assets']=['actor','restroom']
    raw['shots'][0]['assets']=['actor','costume',*crowd,'restroom']
    repaired,changes=p.repair_board_assets(Board.model_validate(raw),prep)
    assert len(repaired.shots[0].assets)==p.MAX_REFERENCE_IMAGES
    assert repaired.shots[0].assets[0]=='actor-packed'
    change=next(item for item in changes if item['action']=='pack_identity_costume_reference')
    assert change['source_asset_ids']==['actor','costume'] and change['stitched_by']=='local_image_tool'
    p.validate_board(repaired,prep)


def test_stitched_boards_reach_models_without_layout_language():
    from test_director import reference_prep
    prep=reference_prep();prep['assets']['actor-packed']={'role':'character','name':'女主 · 日常定装拼接参考',
        'notes':'非 AI 生成的双栏参考板：左栏为「女主」身份图，右栏为「日常定装」服装图；不要把两栏画成两个人。',
        'identity_asset_id':'actor','costume_asset_id':'costume'}
    storyboard=p.storyboard_preproduction(prep)
    prompts=p.shot_prompt_preproduction(prep)
    assert 'actor-packed' not in storyboard['assets']
    assert prompts['assets']['actor-packed']['notes']==p.CONDENSED_REFERENCE_NOTE
    assert '左栏' not in str(prompts) and '不要把两栏画成两个人' not in str(prompts)


def test_board_rejects_visible_character_when_reference_mapping_is_incomplete():
    from types import SimpleNamespace
    prep={'assets':{
        'actor':{'role':'character','name':'灰夹克男人','identity_asset_id':'actor','requires_costume':True},
        'scene':{'role':'scene','name':'死胡同'},
    }}
    shot=SimpleNamespace(id='S04',assets=['scene'],reference_prompt='远处只有灰夹克的背影。')
    with pytest.raises(HTTPException,match='reference_prompt 出现已确认角色.*未绑定其身份图'):
        p.validate_board(SimpleNamespace(shots=[shot]),prep)


def test_board_asset_repair_refuses_ambiguous_scene_names():
    from backend.director import Board
    from test_director import recoverable_board,reference_prep
    raw=recoverable_board();prep=reference_prep()
    prep['assets']['restroom-copy']={**prep['assets']['restroom']}
    repaired,changes=p.repair_board_assets(Board.model_validate(raw),prep)
    assert repaired.shots[1].assets==['actor','costume']
    assert all(change['action']!='bind_unique_named_scene' for change in changes if change['shot_id']=='S02')
    with pytest.raises(HTTPException,match='S02'):p.validate_board(repaired,prep)


def test_references_can_start_storyboard_without_trial_and_reject_stale_inputs(client):
    from sqlalchemy import select
    stamp=prepare(client)
    assert p.approve_references('prep_test',stamp)['mode']=='reference_images'
    with Session() as db:
        assert p.ready(db,'prep_test')['stamp']==stamp
        assert not list(db.scalars(select(Task)))
        assert db.get(Record,'prep_gate_prep_test').data['mode']=='reference_images'
    with Session.begin() as db:db.get(Record,'actor').version+=1
    with Session() as db:
        with pytest.raises(HTTPException):p.ready(db,'prep_test')
