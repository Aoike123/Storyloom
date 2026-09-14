import httpx
from backend import zhihu_stories as z


def test_list_detail_import_preserves_source_and_caches(client,monkeypatch):
    calls=[]
    def get(url,**kwargs):
        calls.append(url)
        assert kwargs['follow_redirects'] is False
        assert 'Authorization' not in kwargs['headers']
        raw=[{'work_id':'123','title':'测试故事','labels':['悬疑'],'extra':'preserve'}] if url.endswith('/list') else {'work_id':'123','author_name':'作者','content':'<script>不应执行</script>故事正文','unknown_field':42}
        return httpx.Response(200,json=raw)
    monkeypatch.setattr(z.httpx,'get',get)
    assert client.get('/api/stories').json()['items'][0]['extra']=='preserve'
    assert client.get('/api/stories').json()['cached'] is True
    assert client.get('/api/stories/123').json()['completeness']=='unknown'
    first=client.post('/api/stories/123/import').json()['story']
    again=client.post('/api/stories/123/import').json()['story']
    assert first['id']==again['id'] and first['author_name']=='作者'
    assert first['raw']['unknown_field']==42
    assert first['labels']==['悬疑']
    assert len(calls)==2
    assert len(client.get('/api/stories/imports').json())==1


def test_no_unlisted_detail_requests(client,monkeypatch):
    calls=[]
    def get(url,**kwargs):calls.append(url);return httpx.Response(200,json=[])
    monkeypatch.setattr(z.httpx,'get',get)
    assert client.get('/api/stories/unknown').status_code==404
    assert calls==[z.BASE+'list']
    assert not z.valid_id('../secret') and not z.valid_id('id?x')


def test_refresh_error_explicitly_returns_stale_cache(client,monkeypatch):
    monkeypatch.setattr(z.httpx,'get',lambda *args,**kwargs:httpx.Response(200,json=[]))
    assert client.get('/api/stories').status_code==200
    monkeypatch.setattr(z.httpx,'get',lambda *args,**kwargs:httpx.Response(503))
    data=client.get('/api/stories?refresh=true').json()
    assert data['stale'] is True and '503' in data['warning']


def test_uncached_failure_has_no_demo_fallback(client,monkeypatch):
    monkeypatch.setattr(z.httpx,'get',lambda *args,**kwargs:httpx.Response(503))
    assert client.get('/api/stories').status_code==502
