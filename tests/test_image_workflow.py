import time
import httpx
import pytest
from backend import image_provider as ip,providers,worker
from backend.db import Session,Task


def test_interrupted_paid_image_requires_review():
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
