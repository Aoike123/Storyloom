import pytest
from backend import billing as b
from backend.db import Session,Record


def test_cached_tokens_cost_and_unknown_prices():
    record={'provider':'llm','status':'completed','usage':{'prompt_tokens':1000000,'completion_tokens':100000,'prompt_cache_hit_tokens':500000},'rates':{'input':2,'cached_input':.2,'output':8}}
    assert b.estimate(record)==pytest.approx(1.9)
    record['rates'].pop('cached_input')
    assert b.estimate(record) is None
    record['usage']['prompt_cache_hit_tokens']=0
    assert b.estimate(record)==pytest.approx(2.8)


def test_price_snapshot_and_idempotent_settlement(client,monkeypatch):
    assert client.post('/api/billing/rates',json={'kind':'image','model':'test','prices':{'image':.5}}).status_code==200
    entry=b.begin('image','test','task')
    client.post('/api/billing/rates',json={'kind':'image','model':'test','prices':{'image':9}})
    b.finish(entry,{'images':1});b.finish(entry,{'images':1})
    with Session() as db:assert b.estimate(db.get(Record,entry).data)==.5
    r=client.get('/api/billing').json()
    assert r['images']==1 and r['estimated_cny']==.5
    assert r['submissions']==1


def test_failed_and_unknown_usage_are_not_free(client):
    entry=b.begin('video','test','task');b.finish(entry,status='rejected')
    result=client.get('/api/billing').json()
    assert result['unpriced_calls']==1 and result['pending_calls']==0
    assert b.estimate({'provider':'llm','status':'completed','usage':{},'rates':{}}) is None
    assert client.post('/api/billing/rates',json={'kind':'video','model':'test','prices':{'video_second':-1}}).status_code==422
