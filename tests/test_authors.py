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

def next_episode_work(db, units=('G01',), finished=('G01',), cut=('G01','G02'), stage='episode_review'):
    """A film waiting at the episode decision, with whatever boards and clips the case needs."""
    db.add(Record(id='work_next',kind='author_project',data={
        'director_id':'pid_next','run_id':'attempt-next','stage':stage,
        'art':'手绘漫画','tone':'温馨',**({'release_id':'release_pid_next'} if stage=='published' else {})}))
    db.add(Record(id='pid_next',kind='director',data={'source_id':'source','status':'approved',
        'segments':{'segments':[{'id':gid} for gid in cut]},
        'units':{gid:{'segment_id':gid} for gid in units}}))
    db.add(Record(id='creative_pid_next',kind='creative_run',data={'stage':stage,'items':[]}))


def rendered(monkeypatch,states):
    """Episode progress without calling the video workspace: the state is what this decision reads."""
    monkeypatch.setattr(c,'rendering_state',lambda pid:[(gid,state) for gid,state in states])


def test_continue_renders_the_episode_that_already_has_a_board(creative,monkeypatch):
    """下一集已经有分镜时，继续要排漫剧生成，而不是从头再排一次分镜。

    这里同时守住一个真实故障：继续把 stage 写在一个已经脱离数据库会话的对象上，保存丢失，
    排出来的节点读到的还是「本集已完成，请先选择发布或继续下一个情节」，于是永远推不动。
    """
    client=TestClient(app)
    with Session.begin() as db:
        next_episode_work(db,units=('G01','G02'))
        # 上一集已经跑完的漫剧节点还在指针上，继续下一集必须接替它，而不是把它当成"仍在运行"。
        db.add(Task(id='rendering_done',kind='author_render',status='completed',message='本集片段已完成',
            payload={'phase':'rendering','work_id':'work_next','run_id':'attempt-next'}))
        db.get(Record,'work_next').data={**db.get(Record,'work_next').data,
            'production_nodes':{'rendering':'rendering_done'}}
    rendered(monkeypatch,[('G01',{'shots':2,'finished':2}),('G02',{'shots':3,'finished':1})])
    assert client.post('/api/author/projects/work_next/episodes/continue',json={}).status_code==422
    response=client.post('/api/author/projects/work_next/episodes/continue',json={'confirm_paid':True})
    assert response.status_code==200,response.text
    assert response.json()=={'queued':True,'segment_id':'G02','phase':'rendering'}
    with Session() as db:
        assert db.get(Record,'creative_pid_next').data['stage']=='storyboard_ready'
        assert db.get(Record,'work_next').data['stage']=='rendering'
        node=db.get(Task,db.get(Record,'work_next').data['production_nodes']['rendering'])
        assert node.kind=='author_render' and node.payload['segment_id']=='G02'
        assert node.id!='rendering_done' and node.payload['revision_of']=='rendering_done'
        assert db.get(Task,'rendering_done').status=='superseded'


def test_continue_plans_the_episode_that_has_no_board_yet(creative,monkeypatch):
    """还没有分镜的情节先规划：继续要按作品自己的状态选步骤，而不是固定排分镜。"""
    client=TestClient(app)
    with Session.begin() as db:next_episode_work(db,units=('G01',))
    rendered(monkeypatch,[('G01',{'shots':2,'finished':2}),('G02',{'shots':0,'finished':0})])
    response=client.post('/api/author/projects/work_next/episodes/continue',json={'confirm_paid':True})
    assert response.status_code==200,response.text
    assert response.json()=={'queued':True,'segment_id':'G02','phase':'storyboarding'}
    with Session() as db:
        assert db.get(Record,'creative_pid_next').data['stage']=='references_ready'
        node=db.get(Task,db.get(Record,'work_next').data['production_nodes']['storyboarding'])
        assert node.kind=='author_storyboard' and node.payload['segment_id']=='G02'


def test_the_next_episode_stays_reachable_after_publishing_one(creative,monkeypatch):
    """发布一集不是这部作品的终点：发布之后必须还能继续做下一集，否则目录永远停在第一集。"""
    client=TestClient(app)
    with Session.begin() as db:next_episode_work(db,units=('G01',),stage='published')
    rendered(monkeypatch,[('G01',{'shots':2,'finished':2}),('G02',{'shots':0,'finished':0})])
    response=client.post('/api/author/projects/work_next/episodes/continue',json={'confirm_paid':True})
    assert response.status_code==200,response.text
    assert response.json()['segment_id']=='G02'
    # 每一集都做完之后，这个入口要说清楚已经无事可做，而不是排一个空节点。
    rendered(monkeypatch,[('G01',{'shots':2,'finished':2}),('G02',{'shots':2,'finished':2})])
    with Session.begin() as db:
        project=db.get(Record,'pid_next')
        project.data={**project.data,'units':{'G01':{'segment_id':'G01'},'G02':{'segment_id':'G02'}}}
        project.version+=1
        db.get(Record,'work_next').data={**db.get(Record,'work_next').data,'stage':'episode_review'}
    done=client.post('/api/author/projects/work_next/episodes/continue',json={'confirm_paid':True})
    assert done.status_code==409 and '全部情节都已生成' in done.json()['detail']


def test_publishing_takes_the_episodes_that_are_done(creative,monkeypatch,sample_video):
    """逐集发布：还有情节在做时，发布要把已完成的情节放出去，而不是被未完成的部分挡住。"""
    import shutil
    from backend import production as prod
    from test_production import saved_clip,setup as production_setup
    shutil.copyfile(sample_video,DATA/'media'/'prod.mp4')
    monkeypatch.setattr(prod,'ready',lambda *a,**k:(None,'test',True))
    monkeypatch.setattr(prod,'matching',lambda t,pid,v,sid=None,token=None:t.payload.get('director_id')==pid and t.payload.get('director_version')==v and (sid is None or t.payload.get('shot_id')==sid))
    monkeypatch.setattr(prod,'validate_references',lambda *a:None)
    with Session.begin() as db:
        production_setup(db)
        order=('G01','G02','G03')
        project=db.get(Record,'director_prod')
        project.data={**project.data,'segments':{'segments':[{'id':gid} for gid in order]},
            'board':{'title':'短场景','shots':[{'id':f'{gid}-S01','segment_id':gid,'motion_prompt':'单镜动作描述',
                'generation_seconds':5,'edit_seconds':3} for gid in order]}}
        for gid in ('G01','G02'):
            saved_clip(db,f'clip_{gid}')
            db.add(Task(id=f'video_{gid}',kind='video',status='completed',
                payload={'director_id':'director_prod','director_version':3,'shot_id':f'{gid}-S01'},
                result={'clip_id':f'clip_{gid}'}))
        db.add(Record(id='creative_director_prod',kind='creative_run',data={'stage':'episode_review','items':[]}))
    release=c.publish('director_prod',c.Publish(confirm=True))
    assert release['published_units']==['G01','G02'] and release['total_units']==3
    assert release['next_unit']=='G03' and release['units_complete'] is False
    assert [entry['shot_id'] for entry in release['entries']]==['G01-S01','G02-S01']
    with Session() as db:
        # 未完成的那一集什么都没发布，也还在制作里。
        assert db.get(Record,'clip_G03') is None
        assert db.get(Record,'creative_director_prod').data['stage']=='published'


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
