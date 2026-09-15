import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.app import app
from backend.db import DATA, Session, Record, Task
from backend import zhihu_stories as z

@pytest.fixture
def story_api(monkeypatch):
    calls=[]
    listing=[{'work_id':str(i),'title':'同名故事' if i<3 else '故事'+str(i),'labels':['脑洞'],'description':'简介'} for i in range(1,13)]
    listing += [{'work_id':'99','title':'都市','labels':['都市']}]
    def fetch(path):
        calls.append(path)
        if path=='list':return listing
        return {'work_id':path,'chapter_name':'第一章','author_name':'原著作者','labels':['脑洞'],'content':'故事原文，不是新建表单。'*10}
    monkeypatch.setattr(z,'fetch',fetch)
    return calls,listing

def release(rid,work_id,media='/media/catalog-test.mp4',created=100,creator=None):
    with Session.begin() as db:
        db.add(Record(id=rid,kind='reader_release',created=created,data={
            'title':'成片','source_title':'同名故事','source_work_id':work_id,
            'entries':[{'clip_id':'clip','media':media,'start':0,'end':4,'shot_id':'shot1'}],
            'creator':creator}))

def test_catalog_all_brainstorm_and_published_first(client,story_api):
    calls,_=story_api
    (DATA/'media/catalog-test.mp4').write_bytes(b'test-video')
    release('older','2',created=100)
    release('latest','2',created=200)
    release('missing','3',media='/media/not-a-real-file.mp4')
    with Session.begin() as db:
        db.add(Record(id='project2',kind='author_project',data={'zhihu_work_id':'2','stage':'published'}))
    data=client.get('/api/reader/catalog').json()
    assert data['count']==12 and data['brainstorm_count']==12 and data['upstream_total']==13
    assert data['ready_count']==1 and data['items'][0]['work_id']=='2'
    assert data['items'][0]['release']['id']=='latest' and data['items'][0]['project_id']=='project2'
    assert next(x for x in data['items'] if x['work_id']=='1')['release'] is None  # Same title is not the same work.
    assert next(x for x in data['items'] if x['work_id']=='3')['release'] is None
    assert calls==['list']  # Never fetch all story bodies just to display the catalog.

def test_open_story_preserves_source_and_resumes_without_paid_tasks(client,story_api,monkeypatch):
    calls,_=story_api
    opened=client.post('/api/author/stories/1/open',json={})
    assert opened.status_code==200,opened.text
    work=opened.json()
    assert work['source']['author_name']=='原著作者' and work['source']['content'].startswith('故事原文')
    assert work['stage']=='style' and work['zhihu_work_id']=='1'
    assert work['source']['completeness']=='unknown'
    with Session.begin() as db:
        row=db.get(Record,work['id'])
        row.data={**row.data,'stage':'assets_review','art':'已选画风'}
    def fail(path):raise AssertionError('Resuming an existing work must not reimport or overwrite its original source.')
    monkeypatch.setattr(z,'fetch',fail)
    resumed=client.post('/api/author/stories/1/open',json={}).json()
    assert resumed['id']==work['id'] and resumed['stage']=='assets_review' and resumed['art']=='已选画风'
    assert resumed['source']==work['source'] and calls==['list','1']
    with Session() as db:
        assert len(list(db.scalars(select(Record).where(Record.kind=='author_project'))))==1
        assert not list(db.scalars(select(Task)))

def test_only_listed_brainstorm_story_can_open(client,story_api):
    assert client.post('/api/author/stories/99/open',json={}).status_code==422
    assert client.post('/api/author/stories/missing/open',json={}).status_code==404
    assert client.post('/api/author/projects',json={'title':'旧入口','content':'正文'*50}).status_code==405

def test_cached_catalog_and_local_releases_survive_upstream_outage(client,story_api,monkeypatch):
    client.get('/api/reader/catalog')
    def fail(path):raise HTTPException(502,'模拟服务不可用')
    monkeypatch.setattr(z,'fetch',fail)
    cached=client.get('/api/reader/catalog?refresh=true').json()
    assert cached['stale'] and cached['cached'] and cached['count']==12 and cached['warning']
    (DATA/'media/catalog-test.mp4').write_bytes(b'test-video')
    release('local-release','1')
    with Session.begin() as db:db.delete(db.get(Record,'zhihu_story_list'))
    offline=client.get('/api/reader/catalog').json()
    assert not offline['catalog_available'] and offline['ready_count']==1
    assert offline['items'][0]['release']['id']=='local-release'

def test_release_requires_safe_complete_media_entries(client,story_api):
    (DATA/'outside-catalog.mp4').write_bytes(b'outside-media-root')
    release('escape','1','/media/../outside-catalog.mp4')
    release('remote','2','https://example.com/clip.mp4')
    with Session.begin() as db:
        db.add(Record(id='empty',kind='reader_release',data={'entries':[],'source_work_id':'3'}))
    assert client.get('/api/reader/catalog').json()['ready_count']==0


def test_public_catalog_never_links_another_accounts_project(story_api,monkeypatch):
    """Published versions are public; private studio ids and progress are owner-scoped."""
    monkeypatch.setenv('STORYLOOM_DEMO_MODE','public')
    monkeypatch.setenv('MODEL_ACCESS_SECRET','test-secret-'+'c'*48)
    browser=TestClient(app)

    def access(key):
        return browser.post('/api/model-access/sessions',json={
            'mode':'own','keys':{'deepseek':key},
        }).json()['token']

    first_token=access('sk-first-catalog-owner')
    second_token=access('sk-second-catalog-owner')
    first_headers={'X-Storyloom-Model-Access':first_token}
    second_headers={'X-Storyloom-Model-Access':second_token}
    first=browser.post('/api/author/stories/1/open',json={},headers=first_headers).json()
    second=browser.post('/api/author/stories/1/open',json={},headers=second_headers).json()
    assert first['id']!=second['id']

    (DATA/'media/catalog-test.mp4').write_bytes(b'test-video')
    release('first-release','1',created=100,creator={'name':'甲的版本','avatar_path':'https://picx.zhimg.com/a.jpg'})
    release('second-release','1',created=200,creator={'name':'乙的版本','avatar_path':'javascript:alert(1)'})
    with Session.begin() as db:
        db.get(Record,first['id']).data={**db.get(Record,first['id']).data,'release_id':'first-release'}
        db.get(Record,second['id']).data={**db.get(Record,second['id']).data,'release_id':'second-release'}

    first_catalog=browser.get('/api/reader/catalog',headers=first_headers).json()
    second_catalog=browser.get('/api/reader/catalog',headers=second_headers).json()
    anonymous_catalog=browser.get('/api/reader/catalog').json()
    first_item=next(item for item in first_catalog['items'] if item['work_id']=='1')
    second_item=next(item for item in second_catalog['items'] if item['work_id']=='1')
    anonymous_item=next(item for item in anonymous_catalog['items'] if item['work_id']=='1')
    assert first_item['project_id']==first['id']
    assert second_item['project_id']==second['id']
    assert anonymous_item['project_id'] is None and anonymous_item['stage'] is None
    assert {item['id'] for item in first_catalog['releases']}=={'first-release','second-release'}
    assert next(item for item in first_catalog['releases'] if item['id']=='first-release')['mine'] is True
    assert next(item for item in first_catalog['releases'] if item['id']=='second-release')['mine'] is False
    assert next(item for item in first_catalog['releases'] if item['id']=='first-release')['creator']=={
        'name':'甲的版本','avatar_path':'https://picx.zhimg.com/a.jpg'}
    assert next(item for item in first_catalog['releases'] if item['id']=='second-release')['creator']=={
        'name':'乙的版本','avatar_path':None}
