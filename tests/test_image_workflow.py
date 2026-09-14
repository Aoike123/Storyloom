import base64
import io
import time
import httpx
import pytest
from PIL import Image
from backend import image_provider as ip,providers,worker,app as application
from backend.db import Session,Record,Task,DATA


def png():
    b=io.BytesIO();Image.new('RGB',(512,512),'blue').save(b,format='PNG');return b.getvalue()


def test_image_generation_saves_asset_and_reviewed_frame_queues_video(client,monkeypatch):
    cfg={'paid_enabled':True,'image_configured':True,'video_configured':True,'paid_used':0,'paid_limit':3,'editable':{'VIDEO_PROVIDER':'minimax'}}
    monkeypatch.setattr(application,'settings',lambda:cfg)
    monkeypatch.setattr(ip,'settings',lambda:cfg)
    monkeypatch.setattr(ip,'reserve_call',lambda *args:None)
    monkeypatch.setenv('IMAGE_PROVIDER','siliconflow')
    monkeypatch.setenv('IMAGE_ENDPOINT','https://api.siliconflow.cn/v1/images/generations')
    monkeypatch.setenv('IMAGE_MODEL','Tongyi-MAI/Z-Image-Turbo')
    def post(url,**kwargs):
        assert kwargs['json']['image_size']=='1024x1024'
        assert kwargs['json']['model']=='Tongyi-MAI/Z-Image-Turbo'
        return httpx.Response(200,json={'images':[{'url':'https://example.com/image.png'}]})
    class Stream:
        def __enter__(self):return httpx.Response(200,content=png(),request=httpx.Request('GET','https://example.com/image.png'))
        def __exit__(self,*args):pass
    monkeypatch.setattr(ip.httpx,'post',post)
    monkeypatch.setattr(ip.httpx,'stream',lambda *args,**kwargs:Stream())
    response=client.post('/api/image',json={'prompt':'漫画角色站在雨夜车站','confirm_paid':True})
    assert response.status_code==200
    assert worker.process_one('image_worker')
    with Session() as db:
        task=db.get(Task,response.json()['id']);assert task.status=='completed'
        aid=task.result['asset_id'];asset=db.get(Record,aid)
        assert asset.data['status']=='pending' and asset.data['demo'] is False
        encoded=ip.local_frame_data(asset.data['media'])
        assert base64.b64decode(encoded.split(',')[1]).startswith(b'\x89PNG')
    body={'prompt':'镜头推进，角色转头','asset_id':aid,'confirm_paid':True}
    assert client.post('/api/video',json=body).status_code==422
    assert client.post('/api/review/'+aid,json={'status':'approved'}).status_code==200
    response=client.post('/api/video',json=body);assert response.status_code==200
    with Session() as db:
        payload=db.get(Task,response.json()['id']).payload
        assert payload['reference_media'][0].endswith('.png')
        assert payload['input_mode']=='reference_images' and 'frame_media' not in payload


def test_image_paid_gate_and_uncertain_restart(client,monkeypatch):
    monkeypatch.setattr(application,'settings',lambda:{'paid_enabled':False})
    assert client.post('/api/image',json={'prompt':'漫画角色站在雨夜车站','confirm_paid':True}).status_code==422
    with Session.begin() as db:
        db.add(Task(id='interrupted_image',kind='image',status='running',lease=time.time()-10,payload={'mode':'live'}))
    assert worker.claim('new_worker') is None
    with Session() as db:assert db.get(Task,'interrupted_image').status=='needs_review'


def test_frame_rejects_path_escape():
    with pytest.raises(providers.ProviderError): ip.local_frame_data('/media/../../.env.local')


def test_invalid_provider_response_not_accepted(monkeypatch):
    monkeypatch.setattr(ip,'settings',lambda:{})
    monkeypatch.setattr(ip,'reserve_call',lambda *args:None)
    monkeypatch.setenv('IMAGE_PROVIDER','siliconflow')
    monkeypatch.setenv('IMAGE_ENDPOINT','https://example.com/images/generations')
    monkeypatch.setattr(ip.httpx,'post',lambda *args,**kwargs:httpx.Response(200,json={'images':[]}))
    with pytest.raises(providers.ProviderError,match='有效图片'):ip.generate_image('test','test')
