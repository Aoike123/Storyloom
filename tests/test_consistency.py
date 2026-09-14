import pytest
from fastapi import HTTPException
from PIL import Image
from backend.db import Session,Record,Task,DATA
from backend import consistency as c,image_provider as ip
from backend.reference_image_model import REFERENCE_IMAGE_MODEL

def prepare(client):
    (DATA/'media').mkdir(exist_ok=True)
    Image.new('RGB',(256,256)).save(DATA/'media'/'ref.png')
    with Session.begin() as db:
        db.add(Record(id='d',kind='director',data={'status':'approved','board':{'shots':[{'id':f'S0{i}'} for i in range(1,5)]}}))
        db.add(Record(id='ref',kind='asset',data={'status':'approved','media':'/media/ref.png'}))
    body={'director_version':1,'style':'Unified illustrated character and room style','bindings':{f'S0{i}':['ref'] for i in range(1,5)},'states':{f'S0{i}':'Same clothing and door position' for i in range(1,5)},'samples':['S01','S02','S03'],'approved':True}
    assert client.post('/api/consistency/d',json=body).status_code==200
    return body

def test_gate_reference_change_and_sample_remake(client):
    body=prepare(client)
    with Session() as db:
        _,token,passed=c.ready(db,'d','S01');assert not passed
        with pytest.raises(HTTPException):c.ready(db,'d','S04')
        with pytest.raises(HTTPException):c.ready(db,'d',batch=True)
        context=c.image_context(db,'d','S01');assert context['reference_media']==['/media/ref.png']
    with Session.begin() as db:
        for i in range(1,4):
            sid=f'S0{i}';aid=f'a{i}'
            db.add(Record(id=aid,kind='asset',data={'status':'approved','director_id':'d','shot_id':sid}))
            db.add(Task(id=f't{i}',kind='image',status='completed',payload={'director_id':'d','shot_id':sid,'consistency_stamp':token},result={'asset_id':aid}))
    assert client.post('/api/consistency/d/approve-samples',json={'stamp':token,'confirm':True,'note':'All three samples visually reviewed'}).status_code==200
    with Session() as db:assert c.ready(db,'d',batch=True)[2]
    with Session.begin() as db:
        db.add(Task(id='remade',kind='image',status='queued',payload={'director_id':'d','shot_id':'S01','consistency_stamp':token}))
    assert not client.get('/api/consistency/d').json()['sample_passed']
    with Session.begin() as db:db.get(Record,'ref').version+=1
    with Session() as db:
        with pytest.raises(HTTPException):c.ready(db,'d','S01')
    assert client.post('/api/consistency/d',json=body).status_code==409

def test_reference_payload_and_separate_billing(client,monkeypatch):
    prepare(client);calls=[];usage=[]
    monkeypatch.setattr(ip,'settings',lambda:None)
    monkeypatch.setenv('IMAGE_PROVIDER','siliconflow');monkeypatch.setenv('IMAGE_ENDPOINT','https://example.test/images')
    monkeypatch.setattr(ip,'reserve_call',lambda kind,task,**kw:usage.append(kw))
    class Response:
        status_code=200
        def json(self):return {'images':[{'url':'https://example.test/image.png'}]}
    monkeypatch.setattr(ip.httpx,'post',lambda *a,**kw:(calls.append(kw['json']) or Response()))
    ip.generate_from_references('scene','task',references=['/media/ref.png']*3,model=REFERENCE_IMAGE_MODEL)
    assert set(calls[0])=={'model','prompt','image','image2','image3','num_inference_steps'}
    assert calls[0]['num_inference_steps']==50
    assert calls[0]['image'].startswith('data:image/png;base64,')
    assert usage==[{'model':REFERENCE_IMAGE_MODEL}]
    with pytest.raises(ip.ProviderError):ip.generate_from_references('scene','task',references=['/media/ref.png'],model='unsupported')
    assert len(calls)==1

def test_no_reference_no_video_or_publish(client,monkeypatch):
    from backend import production
    monkeypatch.setattr(production,'settings',lambda:{'paid_enabled':True,'video_configured':True,'editable':{'VIDEO_PROVIDER':'minimax'}})
    with Session.begin() as db:db.add(Record(id='d',kind='director',data={'status':'approved','board':{'shots':[{'id':'S01'}]}}))
    assert client.post('/api/production/d/shots/S01/video',json={'version':1,'confirm_paid':True}).status_code==409
    assert client.post('/api/production/d/publish',json={'version':1,'confirm':True}).status_code==409
