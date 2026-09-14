import copy
import pytest
from backend import director as d,worker
from backend.db import Session,Record,Task

TEXT='女主看不清鬼怪。她伸手打招呼。鬼怪愣住了。女主继续向前走。'

def board():
    shots=[]
    for i,quote in enumerate(['女主看不清鬼怪','她伸手打招呼','鬼怪愣住了','女主继续向前走']):
        s={k:'明确的镜头设计说明' for k in ['scene','dramatic_action','camera','composition','blocking','continuity_in','continuity_out','viewer_knows','character_knows','withhold','sound','transition','first_frame','motion_prompt','generation_risk']}
        s.update(id=f'S{i+1:02}',purpose=['hook','setup','reaction','payoff'][i],source_quote=quote,size='MS',setup_ids=['S02'] if i==3 else [],edit_seconds=3,generation_seconds=5,dialogue='',assets=['女主','站台'])
        shots.append(s)
    return {'title':'脑洞短场景','scope_note':'只改编已有片段','shots':shots}


def reference_prep():
    return {'stamp':'reference-stamp','assets':{
        'actor':{'role':'character','name':'女主','notes':'固定身份','identity_asset_id':'actor','requires_costume':True},
        'costume':{'role':'costume','name':'日常定装','notes':'固定服装','identity_asset_id':'actor','requires_costume':False},
        'restroom':{'role':'scene','name':'公司洗手间','notes':'固定空间','identity_asset_id':None,'requires_costume':False},
        'apartment':{'role':'scene','name':'公寓卧室','notes':'固定空间','identity_asset_id':None,'requires_costume':False},
    }}


def recoverable_board():
    raw=board()
    for shot in raw['shots']:
        shot['scene']='公司洗手间，西侧隔间内向东望向洗手台'
        shot['assets']=['actor','costume','restroom']
    raw['shots'][1]['assets']=['actor','costume','costume']
    return raw

@pytest.fixture
def setup_source(client,monkeypatch):
    with Session.begin() as db:db.add(Record(id='source_test',kind='story_source',data={'title':'脑洞','labels':['脑洞'],'content':TEXT,'content_hash':'testhash','completeness':'unknown'}))
    monkeypatch.setattr(d,'settings',lambda:{'paid_enabled':True,'llm_configured':True,'paid_limit':3,'paid_used':0,'image_configured':True})
    return client


def test_director_three_stages_persist_and_approval_gates_images(setup_source,monkeypatch):
    client=setup_source
    treatment={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    treatment.update(rules=[{'rule':'女主看不清','quote':'女主看不清鬼怪','consequence':'认知错位'}],boundaries=['完整结局未知'])
    review={'approved':True,'issues':[],'continuity':'可衔接','dramatic_logic':'成立','editability':'可剪辑','production_feasibility':'待视觉审核'}
    answers=iter([treatment,board(),{'shots':[{k:s[k] for k in ('id','first_frame','motion_prompt')} for s in board()['shots']]},review]);calls=[]
    def chat(*args):calls.append(args);return next(answers),{}
    monkeypatch.setattr(d,'chat_json',chat)
    response=client.post('/api/director',json={'source_id':'source_test','confirm_paid':True});assert response.status_code==200
    pid=response.json()['project_id']
    assert client.post('/api/director',json={'source_id':'source_test','confirm_paid':True}).status_code==409
    assert worker.process_one('director_worker')
    p=client.get('/api/director/source_test').json()[0]
    assert len(calls)==1 and p['status']=='awaiting_preproduction' and 'board' not in p
    assert client.post('/api/director/projects/'+pid+'/storyboard',json={'version':p['version'],'confirm_paid':True}).status_code==409
    from backend.db import DATA
    from PIL import Image
    Image.new('RGB',(256,256)).save(DATA/'media'/'prep.png')
    with Session.begin() as db:
        for aid in ('actor','set'):
            db.add(Record(id=aid,kind='asset',data={'name':aid,'media':'/media/prep.png','status':'approved'}))
    config={'style':'固定二维漫画画风和冷色光线','assets':{'actor':{'role':'character','name':'主角','notes':'固定短发和灰色服装'},'set':{'role':'scene','name':'影棚','notes':'门在左侧，窗在右侧，光源来自右侧'} }}
    assert client.post('/api/preproduction/'+pid,json=config).status_code==200
    token=client.get('/api/preproduction/'+pid).json()['stamp']
    with Session.begin() as db:
        db.add(Task(id='fitting',kind='image',status='completed',payload={'preproduction_id':pid,'preproduction_stamp':token,'reference_ids':['actor','set']},result={'asset_id':'fit'}))
        db.add(Record(id='fit',kind='asset',data={'status':'approved','source_task':'fitting','media':'/media/prep.png'}))
    assert client.post('/api/preproduction/'+pid+'/approve',json={'stamp':token,'asset_ids':['fit'],'note':'角色服装比例与影棚布局均已确认','confirm':True}).status_code==200
    b=board()
    for shot in b['shots']:shot['assets']=['actor','set']
    answers=iter([b,{'shots':[{k:s[k] for k in ('id','first_frame','motion_prompt')} for s in b['shots']]},review])
    assert client.post('/api/director/projects/'+pid+'/storyboard',json={'version':p['version'],'confirm_paid':True}).status_code==200
    assert worker.process_one('board_worker')
    p=client.get('/api/director/source_test').json()[0]
    assert len(calls)==4 and p['status']=='pending_review'
    assert calls[1][1]['preproduction']['assets']['set']['notes']==config['assets']['set']['notes']
    path='/api/director/projects/'+pid
    assert client.post(path+'/shots/S01/image',json={'version':p['version'],'confirm_paid':True}).status_code==409
    r=client.post(path+'/approve',json={'version':p['version'],'confirm':True,'note':'已核对所有镜头的连续性'})
    assert r.status_code==200
    image=client.post(path+'/shots/S01/image',json={'version':r.json()['version'],'confirm_paid':True})
    assert image.status_code==409  # Visual references must be approved separately.
    edited=client.post(path+'/edit',json={'version':r.json()['version'],'board':b})
    assert edited.status_code==200 and edited.json()['status']=='pending_review'
    assert edited.json()['review']['approved'] is False


def test_causality_and_source_checks():
    b=board();b['shots'][0]['setup_ids']=['S04'];b['shots'][1]['source_quote']='不存在的原文';b['shots'][2]['edit_seconds']=9
    issues=d.check_board(d.Board.model_validate(b),TEXT)
    assert len(issues)==3


def test_other_tags_and_paid_gate(setup_source):
    client=setup_source
    assert client.post('/api/director',json={'source_id':'source_test'}).status_code==422
    with Session.begin() as db:db.get(Record,'source_test').data={'labels':['言情']}
    assert client.post('/api/director',json={'source_id':'source_test','confirm_paid':True}).status_code==422


def test_unfounded_rule_stops_later_calls(setup_source,monkeypatch):
    t={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    t.update(rules=[{'rule':'凭空能力','quote':'她会瞬间移动','consequence':'无代价'}],boundaries=['未知'])
    calls=[]
    def chat(*args):calls.append(1);return t,{}
    monkeypatch.setattr(d,'chat_json',chat)
    with pytest.raises(d.ProviderError,match='依据'):d.run_director({'source':{'content':TEXT},'brief':'测试'},'t',lambda *x:None)
    assert len(calls)==1

def test_typographic_quote_changes_without_semantic_fuzzy_matching():
    assert d.quote_exists('“她看不清”', '「她\n看不清」')
    assert not d.quote_exists('她看得清', '她看不清')
    assert not d.quote_exists('她已经死了', '她没有死')

def test_reference_binding_uses_actual_source():
    rule=d.Rule(rule='规则',quote='模型摘要不是引文',consequence='后果',source_ref='P001')
    d.bind_sources([rule],d.source_passages(TEXT),'quote')
    assert rule.quote==TEXT
    rule.source_ref='P999'
    with pytest.raises(d.ProviderError):d.bind_sources([rule],d.source_passages(TEXT),'quote')

def test_failed_quote_keeps_treatment_draft(setup_source,monkeypatch):
    t={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    t.update(rules=[{'rule':'规则','quote':'错误引用不会消失','consequence':'后果'}],boundaries=['未知'])
    monkeypatch.setattr(d,'chat_json',lambda *args:(t,{}))
    saved=[]
    with pytest.raises(d.ProviderError):d.run_director({'source':{'content':TEXT},'brief':'测试'},'t',lambda *args:saved.append(args))
    assert next(x for x in saved if x[0]=='treatment')[1]['rules'][0]['quote']=='错误引用不会消失'

def test_invalid_board_preserves_diagnostics(setup_source,monkeypatch):
    treatment={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    treatment.update(rules=[{'rule':'规则','quote':TEXT,'consequence':'后果'}],boundaries=['未知'])
    raw=board();raw['shots'][0]['size']='INVALID'
    responses=iter([treatment,raw]);saved=[]
    monkeypatch.setattr(d,'chat_json',lambda *a:(next(responses),{}))
    with pytest.raises(d.ProviderError,match='shots.0.size'):
        d.run_director({'source':{'content':TEXT},'brief':'测试'},'test',lambda *a:saved.append(a))
    diagnostic=next(x[1] for x in saved if x[0]=='board_diagnostics')
    assert diagnostic['raw']==raw
    assert diagnostic['errors'][0]['field']=='shots.0.size'
    assert 'input' not in diagnostic['errors'][0]


def test_saved_board_skips_storyboard_model_and_repairs_unique_scene_binding(monkeypatch):
    raw=recoverable_board();prep=reference_prep();nodes=[];saved=[]
    treatment={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    treatment.update(rules=[{'rule':'女主看不清','quote':'女主看不清鬼怪','consequence':'认知错位'}],boundaries=['完整结局未知'])
    def node(_chat,name,payload,*args,**kwargs):
        nodes.append(name)
        if name=='storyboard':pytest.fail('Saved storyboard must not be resubmitted')
        if name=='shot_prompts':
            return {'shots':[{'id':s['id'],'reference_prompt':'完整静态镜头参考图提示词','motion_prompt':'完整单镜运动提示词'} for s in raw['shots']]},{}
        assert name=='storyboard_review'
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}
    monkeypatch.setattr(d,'call_node',node)
    result=d.run_director({'stage':'board','source':{'content':TEXT},'brief':'测试','treatment':treatment,
                           'preproduction':prep,'professional_prompts':True,'saved_board':raw},
                          'saved-board',lambda *args:saved.append(args))
    assert result['approved'] is True and nodes==['shot_prompts','storyboard_review']
    repaired=next(value for key,value,*_ in saved if key=='board' and value['shots'][1]['id']=='S02')
    assert repaired['shots'][1]['assets']==['actor','costume','restroom']
    changes=next(value for key,value,*_ in saved if key=='board_repairs')['changes']
    assert {change['action'] for change in changes}=={'remove_duplicate_assets','bind_unique_named_scene'}


def test_frame_remake_points_to_the_previous_current_frame(client,monkeypatch):
    from backend import consistency
    monkeypatch.setattr(d,'settings',lambda:{'paid_enabled':True,'image_configured':True})
    context={'consistency_stamp':'visual-v1','reference_media':['/media/ref.png'],
        'reference_assets':[{'id':'ref','version':1}],'image_model':'test-model',
        'visual_style':'固定漫画画风','continuity_state':'人物位置保持一致'}
    monkeypatch.setattr(consistency,'image_context',lambda *args:context)
    shot={'id':'S01','reference_prompt':'静态镜头构图提示词','motion_prompt':'人物缓慢向前移动',
        'generation_seconds':5,'edit_seconds':3}
    with Session.begin() as db:
        db.add(Record(id='frame-project',kind='director',version=2,data={
            'status':'approved','board':{'title':'当前分镜','shots':[shot]}}))
        db.add(Task(id='old-frame',kind='image',status='completed',payload={
            'director_id':'frame-project','director_version':2,'shot_id':'S01','consistency_stamp':'visual-v1'},
            result={'media':'/media/old.png','asset_id':'old-frame-asset'}))
    response=client.post('/api/director/projects/frame-project/shots/S01/image',json={
        'version':2,'remake':True,'confirm_paid':True})
    assert response.status_code==200,response.text
    with Session() as db:
        replacement=db.get(Task,response.json()['id'])
        assert replacement.id!='old-frame' and replacement.payload['revision_of']=='old-frame'
