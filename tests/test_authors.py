import time

from fastapi.testclient import TestClient
from backend.app import app
from backend.db import Session,Record,Task,DATA
from backend import authors,creative as c,worker,director as d,preproduction as pp
from test_creative import creative,drain
from test_director import board,segment_plan
from test_catalog import story_api
from backend.production_nodes import KINDS as PRODUCTION_KINDS


def own_key_client(monkeypatch,key):
    """A browser paying with its own key: one payer identity per session."""
    monkeypatch.setenv('MODEL_ACCESS_SECRET','test-secret-'+'a'*48)
    client=TestClient(app)
    client.headers.update({'X-Storyloom-Model-Access': ''})
    response=client.post('/api/model-access/sessions',json={'mode':'own','keys':{'deepseek':key}})
    assert response.status_code==200,response.text
    client.headers.update({'X-Storyloom-Model-Access':response.json()['token']})
    return client


def test_each_payer_sees_only_its_own_works(story_api,monkeypatch):
    """Two visitors must not share one work stream, one film, or one wallet's work."""
    first=own_key_client(monkeypatch,'sk-first-visitor-key')
    second=own_key_client(monkeypatch,'sk-second-visitor-key')
    opened=first.post('/api/author/stories/1/open',json={})
    assert opened.status_code==200
    pid=opened.json()['id']
    assert str(opened.json()['owner']).startswith('access:')
    assert [item['id'] for item in second.get('/api/author/projects').json()]==[]
    assert second.get('/api/author/projects/'+pid).status_code==404
    assert first.get('/api/author/projects').json()[0]['id']==pid
    other=second.post('/api/author/stories/1/open',json={})
    assert other.status_code==200 and other.json()['id']!=pid
    assert {item['id'] for item in second.get('/api/author/projects').json()}=={other.json()['id']}


def test_an_ownerless_project_is_never_reachable_or_takeable(story_api,monkeypatch):
    """作品按付款身份取独立 ID；没有归属者的记录不会出现在列表里，也不能被顶替。"""
    monkeypatch.setenv('STORYLOOM_DEMO_MODE','public')
    with Session.begin() as db:
        db.add(Record(id='work_zhihu_ownerless',kind='author_project',data={'title':'无名作品','stage':'style'}))
    visitor=own_key_client(monkeypatch,'sk-arriving-visitor')
    assert visitor.get('/api/author/projects').json()==[]
    assert visitor.get('/api/author/projects/work_zhihu_ownerless').status_code==404
    opened=visitor.post('/api/author/stories/7/open',json={})
    assert opened.status_code==200 and opened.json()['id']!= 'work_zhihu_ownerless'
    assert opened.json()['owner'].startswith('access:')
    # 另一个付款身份会拿到自己的工作，既看不到也接管不了前一位的作品。
    stranger=own_key_client(monkeypatch,'sk-stranger-visitor')
    assert stranger.get('/api/author/projects/'+opened.json()['id']).status_code==404
    assert stranger.post('/api/author/stories/7/open',json={}).json()['id']!=opened.json()['id']


def test_public_browser_cannot_create_an_ownerless_project(story_api,monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE','public')
    visitor=TestClient(app)
    response=visitor.post('/api/author/stories/1/open',json={})
    assert response.status_code==401
    assert '登录' in response.json()['detail']
    with Session() as db:
        assert not list(db.query(Record).filter(Record.kind=='author_project').all())

def test_attaching_own_keys_keeps_a_signed_in_accounts_works(story_api,monkeypatch):
    """Paying with own keys must not change who owns the works: that is the account, not the payer."""
    monkeypatch.setenv('MODEL_ACCESS_SECRET','test-secret-'+'a'*48)
    monkeypatch.setenv('STORYLOOM_DEMO_MODE','public')
    from backend import model_access
    from backend.public_limits import clear_request_limit_state
    clear_request_limit_state()
    client=TestClient(app)
    # The browser is signed in through the Zhihu cookie the callback sets. The cookie value must
    # satisfy the session-id pattern the login flow issues.
    import backend.zhihu_oauth as zo
    session_id='author-session-0123456789abcdef'
    model_access.create_account_session(session_id,'7001')
    with Session.begin() as db:
        # The OAuth callback stores the login record; the bean wallet session is separate.
        db.add(Record(id=zo._session_record_id(session_id),kind=zo.SESSION_KIND,data={
            'uid':'7001','token':'stored-server-side','expires_at':time.time()+3600}))
    login={zo.COOKIE_NAME:session_id}
    assert client.get('/api/zhihu/status',cookies=login).json()['account']['uid']=='7001'
    opened=client.post('/api/author/stories/1/open',json={},cookies=login)
    assert opened.status_code==200
    pid=opened.json()['id']
    assert opened.json()['owner']=='account:7001'
    # Now switch to own keys; the account is still signed in, so the work stays visible.
    token=client.post('/api/model-access/sessions',
                      json={'mode':'own','keys':{'deepseek':'sk-visitor-abcdefgh'}}).json()['token']
    client.headers.update({'X-Storyloom-Model-Access':token})
    assert [item['id'] for item in client.get('/api/author/projects',cookies=login).json()]==[pid]
    assert client.get('/api/author/projects/'+pid,cookies=login).status_code==200
    assert client.post('/api/author/stories/1/open',json={},cookies=login).json()['id']==pid


def test_local_mode_without_accounts_keeps_the_workspace_usable(story_api):
    a=TestClient(app);b=TestClient(app)
    novel={'title':'演示小说','content':'我发现所有人每天都会失去一段记忆，只有我能记住昨天发生的一切。今天，我终于找到了原因。'}
    r=a.post('/api/author/stories/1/open',json={});assert r.status_code==200
    pid=r.json()['id']
    assert r.json()['owner'] is None
    assert a.get('/api/author/projects').json()==b.get('/api/author/projects').json()
    assert b.get('/api/author/projects/'+pid).json()['id']==pid
    assert b.get('/api/author/projects/missing').status_code==404
    assert b.get('/api/settings').status_code==200
    (DATA/'media/unpublished.png').write_bytes(b'image')
    assert a.get('/media/unpublished.png').status_code==200
    assert b.get('/media/unpublished.png').status_code==200
    assert not a.cookies and not b.cookies


def test_publish_snapshots_only_public_creator_fields(monkeypatch):
    """Market attribution includes the maker's name/avatar, never private account data."""
    import backend.zhihu_oauth as zo
    session_id='publisher-session-0123456789abcdef'
    with Session.begin() as db:
        db.add(Record(id=zo._session_record_id(session_id),kind=zo.SESSION_KIND,data={
            'uid':'8123','fullname':'版本制作者','avatar_path':'https://picx.zhimg.com/maker.jpg',
            'headline':'不应进入发布快照','token':'stored-server-side','expires_at':time.time()+3600}))
        db.add(Record(id='published-work',kind='author_project',data={
            'stage':'film_review','director_id':'published-director'}))
        db.add(Record(id='published-release',kind='reader_release',data={'entries':[]}))
    monkeypatch.setattr(c,'publish',lambda pid,body:{'id':'published-release'})
    browser=TestClient(app)
    response=browser.post('/api/author/projects/published-work/publish',json={'confirm':True},
                          cookies={zo.COOKIE_NAME:session_id})
    assert response.status_code==200,response.text
    with Session() as db:
        release=db.get(Record,'published-release')
        project=db.get(Record,'published-work')
        assert release.data['creator']=={
            'name':'版本制作者','avatar_path':'https://picx.zhimg.com/maker.jpg'}
        assert set(release.data['creator'])=={'name','avatar_path'}
        assert project.data['release_id']=='published-release'


def test_author_flow_stops_only_for_assets_and_film(creative,monkeypatch,sample_video):
    import shutil
    client=creative
    assert client.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'温馨','confirm_paid':True}).status_code==200
    drain()
    a=TestClient(app)
    with Session.begin() as db:
        db.add(Record(id='work',kind='author_project',data={'director_id':'pid','stage':'preparing','art':'手绘漫画','tone':'温馨'}))
        # 情节切割在导演阐述阶段完成；确认它之前，作者流程会停在情节确认，不会开始画图。
        project=db.get(Record,'pid');project.data={**project.data,'segments':segment_plan()}
    assert authors.flow('unused',{'work_id':'work','phase':'preparing'})
    assert a.get('/api/author/projects/work').json()['stage']=='assets_review'
    assert a.post('/api/author/projects/work/generate',json={'confirm_paid':True}).status_code==422
    assert a.post('/api/author/projects/work/generate',json={'confirm':True,'confirm_paid':True}).status_code==200
    assert a.post('/api/author/projects/work/generate',json={'confirm':True,'confirm_paid':True}).status_code==409
    # A single-character shot stays as three separate identity, costume, and scene references.
    worker.process_one('author-test')
    pre=pp.get('pid')['config'];b=board()
    identity=next((aid for aid,spec in pre['assets'].items() if spec['role']=='character' and not spec.get('costume_asset_id')))
    costume=next((aid for aid,spec in pre['assets'].items() if spec['role']=='costume' and spec.get('identity_asset_id')==identity))
    scene=next((aid for aid,spec in pre['assets'].items() if spec['role']=='scene'))
    packed=[aid for aid,spec in pre['assets'].items() if spec['role']=='character' and spec.get('costume_asset_id')]
    assert packed==[]
    for shot in b['shots']:shot['assets']=[identity,costume,scene]
    answers=iter([b,{'shots':[{k:s[k] for k in ('id','first_frame','motion_prompt')} for s in b['shots']]},{'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'}])
    monkeypatch.setattr(d,'chat_json',lambda *args:(next(answers),{}))
    # Advance supervisor leases without sleeping or calling any paid provider.
    from sqlalchemy import select
    for _ in range(12):
        with Session.begin() as db:
            for t in db.scalars(select(Task).where(Task.kind.in_(['author_flow',*PRODUCTION_KINDS]),Task.status=='waiting')):t.lease=0
        if c.get_status('pid')=='videos_review':break
        worker.process_one('author-test')
        # Stop before executing the real video provider; image and LLM are mocked.
        for _ in range(20):
            with Session() as db:
                if list(db.scalars(select(Task).where(Task.kind=='video'))):break
            if not worker.process_one('author-test'):break
    assert c.get_status('pid')=='videos_review'
    with Session() as db:
        assert not [task for task in db.scalars(select(Task).where(Task.kind=='image')) if task.payload.get('preproduction_id')=='pid']
    # 每个片段用真实的保存路径登记：片段、存储记录与播放文件一起落库，和供应商返回时一致。
    with Session() as db:
        videos=[(t.id,dict(t.payload),{**dict(t.result),'provider_id':f'provider-{t.id}'})
                for t in db.scalars(select(Task).where(Task.kind=='video'))]
    with Session.begin() as db:
        for tid,_,_ in videos:
            task=db.get(Task,tid);task.status='running';task.owner='clip-owner'
    for tid,payload,result in videos:
        worker.save_video_result(tid,'clip-owner',payload,result,sample_video)
    with Session.begin() as db:
        for t in db.scalars(select(Task).where(Task.kind.in_(PRODUCTION_KINDS),Task.status=='waiting')):t.lease=0
    assert worker.process_one('author-test')
    with Session() as db:
        nodes=list(db.scalars(select(Task).where(Task.kind.in_(PRODUCTION_KINDS))))
        assert len(nodes)==2 and all(node.status=='completed' for node in nodes)
    work=a.get('/api/author/projects/work').json()
    assert work['stage']=='episode_review'
    assert work['episodes'][0]['render_complete'] is True and work['episodes'][0]['published'] is False
    assert a.get('/api/reader/stories').json()==[]
    assert a.post('/api/author/projects/work/publish',json={'confirm':False}).status_code==422
    result=a.post('/api/author/projects/work/publish',json={'confirm':True})
    assert result.status_code==200,result.text
    assert len(a.get('/api/reader/stories').json())==1
    assert a.post('/api/author/projects/work/publish',json={'confirm':True}).json()['id']==result.json()['id']

def test_the_cut_is_reviewed_before_any_artwork_is_drawn(creative):
    """切割先于人审：没有确认情节之前不生成任何人物或场景，确认后才开始第一幕。"""
    from test_director import segment_plan
    with Session.begin() as db:
        db.add(Record(id='work',kind='author_project',data={'director_id':'pid','stage':'preparing',
            'art':'手绘漫画','tone':'温馨'}))
        project=db.get(Record,'pid')
        project.data={**project.data,'segments':segment_plan((('P001',),('P002',)))}
        # 设计任务尚未开始，流程应停在情节确认而不是直接画图。
    client=TestClient(app)
    assert authors.flow('unused',{'work_id':'work','phase':'preparing'})
    stopped=client.get('/api/author/projects/work').json()
    assert stopped['stage']=='segments_review'
    assert [episode['segment_id'] for episode in stopped['episodes']]==['G01','G02']
    assert all(episode['storyboarded'] is False for episode in stopped['episodes'])
    assert [episode['characters'] for episode in stopped['episodes']]==[['女主'],['女主']]
    assert client.post('/api/author/projects/work/segments/approve',json={}).status_code==422
    approved=client.post('/api/author/projects/work/segments/approve',json={'confirm':True})
    assert approved.status_code==200,approved.text
    assert approved.json()['episodes']==['G01','G02']
    assert client.post('/api/author/projects/work/segments/approve',json={'confirm':True}).status_code==409
    with Session() as db:
        assert db.get(Record,'work').data['stage']=='preparing'
        audit=[row for row in db.query(Record).filter(Record.kind=='audit').all()
               if row.data.get('action')=='segments_approved']
        assert audit and audit[0].data['episodes']==['G01','G02']


def test_draft_does_not_schedule_paid_tasks(story_api):
    a=TestClient(app)
    assert a.post('/api/author/stories/1/open',json={}).status_code==200
    from sqlalchemy import select
    with Session() as db:assert not list(db.scalars(select(Task)))

def test_interrupted_supervisor_does_not_replay():
    with Session.begin() as db:
        db.add(Task(id='interrupted-author',kind='author_flow',status='running',lease=0,payload={'work_id':'unused'}))
    assert worker.claim('new-worker') is None
    with Session() as db:assert db.get(Task,'interrupted-author').status=='needs_review'
