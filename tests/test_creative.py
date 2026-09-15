import pytest
from PIL import Image
from sqlalchemy import select
from backend.db import Session,Record,Task,DATA
from backend import creative as c,director as d,preproduction as pp,production as prod,worker
from test_director import board,TEXT,segment_plan
from asset_spec_fixtures import design_response,character

@pytest.fixture
def creative(client,monkeypatch):
    cfg=lambda:{'paid_enabled':True,'llm_configured':True,'image_configured':True,'video_configured':True,'editable':{'VIDEO_PROVIDER':'minimax'}}
    # preproduction 不再自行收费（试拍已移除），所以只有这三处还需要替换付费状态。
    for mod in (c,d,prod):monkeypatch.setattr(mod,'settings',cfg)
    treatment={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    treatment.update(rules=[{'rule':'规则','quote':TEXT,'consequence':'后果'}],boundaries=['未知'])
    with Session.begin() as db:
        db.add(Record(id='source',kind='story_source',data={'title':'原作','labels':['脑洞'],'content':TEXT,'content_hash':'x'}))
        db.add(Task(id='origin',kind='director',status='completed',payload={'source':{'content':TEXT,'title':'原作'}}))
        db.add(Record(id='pid',kind='director',data={'task_id':'origin','source_id':'source','brief':'短场景','status':'awaiting_preproduction','treatment':treatment,'requires_preproduction':True}))
    monkeypatch.setattr(c,'chat_json',design_response)
    monkeypatch.setattr(worker,'generate_image',lambda *a,**k:'https://example.test/image')
    monkeypatch.setattr(worker,'generate_from_references',lambda *a,**k:'https://example.test/image')
    monkeypatch.setattr(worker,'submit_video',lambda *a,**k:'provider-video-task')
    def save(url,tid):
        Image.new('RGB',(256,256)).save(DATA/'media'/f'{tid}.png')
        return f'/media/{tid}.png'
    monkeypatch.setattr(worker,'save_image',save)
    return client

def drain():
    for _ in range(30):
        if not worker.process_one('creative-test'):break

def test_base_assets_are_ready_and_the_removed_fitting_stages_are_gone(creative):
    """基础素材照常生成；定装与试拍已从代码中删除，既没有推进入口，也不会创建生图任务。

    当前线路从"确认基础素材"到分镜、视频、审片的完整流程由
    ``test_authors.test_author_flow_stops_only_for_assets_and_film`` 覆盖。
    """
    client=creative
    assert client.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'温馨','confirm_paid':True}).status_code==200
    drain();data=client.get('/api/creative/pid').json()
    assert len(data['items'])==3 and all(i['asset'] for i in data['items'])
    assert data['stage']=='assets_review' and data['production']['shots']==[]
    for stage in ('assets_review','fittings_review','trials_review'):
        response=client.post('/api/creative/pid/continue',json={'stage':stage,'confirm_review':True,'confirm_paid':True})
        assert response.status_code==404
    with Session() as db:
        assert not [t for t in db.scalars(select(Task).where(Task.kind=='image'))
                    if t.payload.get('asset_kind')=='dressed_character' or t.payload.get('preproduction_id')]

def test_natural_language_revision_retains_old_image(creative,monkeypatch):
    client=creative
    client.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'温馨','confirm_paid':True});drain()
    item=client.get('/api/creative/pid').json()['items'][0]
    with Session.begin() as db:
        db.add(Record(id='prep_pid',kind='preproduction',data={'versions':{},'assets':{}}))
        db.add(Record(id='visual_pid',kind='visual_config',data={'approved':True,'bindings':{}}))
    changed=character();changed['appearance']['hair_shape']='耳上直短发，偏左分缝'
    monkeypatch.setattr(c,'chat_json',lambda system,payload,*a:({'items':[{'asset_index':0,'prompt':payload['assets'][0]['render_contract']}]} if payload['schema']['title']=='AssetPromptBatch' else changed,{}))
    r=client.post('/api/creative/pid/feedback',json={'task_id':item['task']['id'],'text':'头发短一些','confirm_paid':True})
    assert r.status_code==200;drain()
    new=client.get('/api/creative/pid').json()['items'][0]
    assert new['task']['id']!=item['task']['id']
    with Session() as db:
        assert db.get(Record,'prep_pid').data.get('invalidated_at')
        assert db.get(Record,'visual_pid').data.get('approved') is False
        assert db.get(Record,item['asset']['id']) is not None
        payload=db.get(Task,new['task']['id']).payload
        assert payload['input_mode']=='text_to_image' and 'reference_media' not in payload
        assert db.get(Record,new['asset']['id']).data['status']=='pending'


def test_video_feedback_rewrites_complete_prompt_and_submits_a_new_task(creative,monkeypatch):
    client=creative
    client.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'温馨','confirm_paid':True});drain()
    with Session.begin() as db:
        db.add(Task(id='old-video',kind='video',status='completed',payload={
            'mode':'live','director_id':'pid','title':'镜头 S01','prompt':'原视频完整提示词',
            'input_mode':'reference_images','reference_media':['/media/approved-shot.png'],
            'input_snapshot':{'prompt':'原视频完整提示词','asset_bindings':[]}},
            result={'media':'/media/old-video.mp4'}))
    def revise(system,payload,*args):
        assert payload['original']=='原视频完整提示词'
        assert payload['feedback']=='镜头推进慢一些'
        return {'prompt':'依据已批准参考图片重新生成：镜头缓慢推进，人物动作与结束状态保持连续。'},{}
    monkeypatch.setattr(c,'chat_json',revise)
    response=client.post('/api/creative/pid/feedback',json={
        'task_id':'old-video','text':'镜头推进慢一些','confirm_paid':True})
    assert response.status_code==200
    assert worker.process_one('video-revision-test')
    with Session() as db:
        parent=db.get(Task,response.json()['id']);new=db.get(Task,parent.result['task_id'])
        assert parent.status=='completed' and new.kind=='video' and new.status=='queued'
        assert new.payload['revision_of']=='old-video'
        assert new.payload['reference_media']==['/media/approved-shot.png']
        assert new.payload['prompt']==new.payload['input_snapshot']['prompt']
        assert new.payload['regeneration_mode']=='full_prompt'
        assert new.payload['node_skill']['node']=='video_revision'
        assert '/media/old-video.mp4' not in str(new.payload)
