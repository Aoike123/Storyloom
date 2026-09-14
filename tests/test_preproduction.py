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
    with pytest.raises(HTTPException):p.validate_board(board,{'assets':{'scene':{'role':'scene'}}})


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
