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


def segment_plan(groups=(('P001',),),shot_budget=4):
    """A saved微小说切割 result so storyboard tests exercise chunked generation."""
    return {'title':'原文片段切割','overall_arc':'按原文顺序串联全部片段',
            'segments':[{'id':f'G{index+1:02}','title':f'片段 {index+1}','source_refs':list(refs),
                         'beat':'setup' if index==0 else 'development','purpose':'交代本片段信息，并把悬念留给下一片段',
                         'characters':['女主'],'location':'公司洗手间','shot_budget':shot_budget,
                         'continuity_out':'人物留在原地，异常尚未解释'} for index,refs in enumerate(groups)]}

@pytest.fixture
def setup_source(client,monkeypatch):
    with Session.begin() as db:db.add(Record(id='source_test',kind='story_source',data={'title':'脑洞','labels':['脑洞'],'content':TEXT,'content_hash':'testhash','completeness':'unknown'}))
    monkeypatch.setattr(d,'settings',lambda:{'paid_enabled':True,'llm_configured':True,'image_configured':True})
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
    with Session.begin() as db:
        row=db.get(Record,pid);row.data={**row.data,'segments':segment_plan()}
    assert client.post('/api/director/projects/'+pid+'/storyboard',json={'version':p['version'],'confirm_paid':True}).status_code==200
    assert worker.process_one('board_worker')
    p=client.get('/api/director/source_test').json()[0]
    assert len(calls)==4 and p['status']=='pending_review'
    assert calls[1][1]['preproduction']['assets']['set']['notes']==config['assets']['set']['notes']
    path='/api/director/projects/'+pid
    # 镜头参考图步骤已移除：分镜不再接受单镜图片生成请求。
    assert client.post(path+'/shots/S01/image',json={'version':p['version'],'confirm_paid':True}).status_code==404
    r=client.post(path+'/approve',json={'version':p['version'],'confirm':True,'note':'已核对所有镜头的连续性'})
    assert r.status_code==200
    assert client.post(path+'/shots/S01/image',json={'version':r.json()['version'],'confirm_paid':True}).status_code==404
    edited=client.post(path+'/edit',json={'version':r.json()['version'],'board':b})
    assert edited.status_code==200 and edited.json()['status']=='pending_review'
    assert edited.json()['review']['approved'] is False


def test_causality_and_source_checks():
    b=board();b['shots'][0]['setup_ids']=['S04'];b['shots'][1]['source_quote']='不存在的原文';b['shots'][2]['edit_seconds']=9
    issues=d.check_board(d.Board.model_validate(b),TEXT)
    assert len(issues)==3


def test_causality_error_gives_a_concrete_model_correction():
    b=board();b['shots'][1]['purpose']='reveal';b['shots'][1]['setup_ids']=[]
    issues=d.check_board(d.Board.model_validate(b),TEXT)
    issue=next(item for item in issues if '揭示或回收缺少前序铺垫' in item)
    assert 'setup_ids' in issue and 'S01' in issue
    assert 'purpose' in issue and 'rule' in issue


def test_board_rejects_source_order_rollback():
    b=board()
    for shot,source_ref in zip(b['shots'],['P001','P004','P002','P003']):shot['source_ref']=source_ref
    issues=d.check_board(d.Board.model_validate(b),TEXT)
    assert any('S03 原文顺序倒退' in issue and 'S02（P004）' in issue for issue in issues)
    assert any('S04 原文顺序倒退' in issue and '保持原著因果顺序' in issue for issue in issues)


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
    assert len(calls)==4

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
    diagnostic=[x for x in saved if x[0]=='treatment_diagnostics'][-1][1]
    assert diagnostic['raw']['rules'][0]['quote']=='错误引用不会消失'
    assert len(diagnostic['attempts'])==4

def test_invalid_board_preserves_diagnostics(setup_source,monkeypatch):
    treatment={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    treatment.update(rules=[{'rule':'规则','quote':TEXT,'consequence':'后果'}],boundaries=['未知'])
    raw=board();raw['shots'][0]['size']='INVALID'
    saved=[]
    plan=segment_plan()
    monkeypatch.setattr(d,'chat_json',lambda _system,payload,*a:((
        treatment if payload['schema']['title']=='Treatment' else plan if payload['schema']['title']=='SegmentPlan' else raw),{}))
    with pytest.raises(d.ProviderError,match='shots.0.size'):
        d.run_director({'source':{'content':TEXT},'brief':'测试'},'test',lambda *a:saved.append(a))
    diagnostic=[x[1] for x in saved if x[0] in ('board_diagnostics','board_chunk_diagnostics')][-1]
    assert diagnostic['segment_id']=='G01'
    assert diagnostic['raw']==raw
    assert diagnostic['errors'][0]['field']=='shots.0.size'
    assert 'input' not in diagnostic['errors'][0]
    assert len(diagnostic['attempts'])==4


def test_storyboard_schema_and_retry_feedback_enforce_reference_capacity(setup_source,monkeypatch):
    assert d.Shot.model_json_schema()['properties']['assets']['maxItems']==12
    treatment={k:'设计依据' for k in ['premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy']}
    treatment.update(rules=[{'rule':'规则','quote':TEXT,'consequence':'后果'}],boundaries=['未知'])
    prep=reference_prep();prep['assets']['actor']['requires_costume']=False;prep['assets'].pop('costume')
    crowd={f'extra{i}':{'role':'character','name':f'同事{i}','identity_asset_id':f'extra{i}',
        'requires_costume':False} for i in range(1,9)}
    prep['assets'].update(crowd)
    invalid=board();valid=board()
    for shot in invalid['shots']:shot['assets']=['actor','restroom']
    invalid['shots'][0]['assets']=['actor',*crowd,'restroom']
    for shot in valid['shots']:shot['assets']=['actor','restroom']
    systems=[];board_payloads=[];answers=iter([invalid,valid,{
        'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'}])
    def chat(system,payload,*args,**kwargs):
        systems.append(system)
        if payload['schema']['title']=='Board':board_payloads.append(payload)
        return next(answers),{}
    monkeypatch.setattr(d,'chat_json',chat)
    result=d.run_director({'stage':'board','source':{'content':TEXT},'brief':'测试','treatment':treatment,
                           'preproduction':prep,'segments':segment_plan(),'retry_feedback':'上一次节点的具体错误'},'retry-board',lambda *args:None)
    assert result['approved'] is True and len(board_payloads)==2
    assert board_payloads[0]['preproduction']['asset_binding_contract']['character_reference_sets']
    assert board_payloads[0]['preproduction']['previous_node_error']=='上一次节点的具体错误'
    assert 'S01 的 assets 完整绑定为 10 张' in systems[1]
    assert '视频模型每镜最多接收 9 张参考图' in systems[1]
    assert '多人内容必须拆成单人反打、近景或空场景镜头' in systems[1]


def test_saved_board_skips_storyboard_model_and_repairs_unique_scene_binding(monkeypatch):
    raw=recoverable_board();prep=reference_prep();nodes=[];saved=[]
    raw['shots'][1].update(purpose='reveal',setup_ids=[],continuity_in='承接 S01 已建立的异常规则')
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
    assert repaired['shots'][1]['setup_ids']==['S01']
    changes=next(value for key,value,*_ in saved if key=='board_repairs')['changes']
    assert {change['action'] for change in changes}=={
        'bind_explicit_setup_reference','remove_duplicate_assets','bind_unique_named_scene'}


def test_shot_reference_image_endpoint_is_removed(client):
    with Session.begin() as db:
        db.add(Record(id='frame-project',kind='director',version=2,data={
            'status':'approved','board':{'title':'当前分镜','shots':[{'id':'S01'}]}}))
    response=client.post('/api/director/projects/frame-project/shots/S01/image',json={
        'version':2,'remake':True,'confirm_paid':True})
    assert response.status_code==404


def test_invented_source_quote_is_rejected_and_precise_citation_is_kept():
    """Replacing the quote before validation is what used to make this check unable to fire."""
    passages=d.source_passages(TEXT)
    shot={'id':'S01','scene':'a','purpose':'hook','source_quote':'编剧自己编的一句台词',
        'dramatic_action':'a','size':'MS','camera':'a','composition':'a','blocking':'a',
        'continuity_in':'a','continuity_out':'a','viewer_knows':'a','character_knows':'a',
        'withhold':'a','setup_ids':[],'edit_seconds':3,'generation_seconds':5,'dialogue':'',
        'sound':'a','transition':'a','reference_prompt':'abcde','motion_prompt':'abcde',
        'assets':['a'],'generation_risk':'a','source_ref':'P001'}
    board=d.Board.model_validate({'title':'t','scope_note':'s','shots':[shot]})
    with pytest.raises(d.ProviderError,match='在原文中找不到'):
        d.bind_sources(board.shots,passages,'source_quote',TEXT)

    # A quote that really exists in the story, but under a different P number, is rebound and
    # reported instead of being passed off as a faithful citation.
    longer='甲'*260+'女主看不清鬼怪。'
    long_passages=d.source_passages(longer)
    assert len(long_passages)>1
    faithful=d.Board.model_validate({'title':'t','scope_note':'s','shots':[{
        **{k:v for k,v in shot.items()},'source_quote':'女主看不清鬼怪','source_ref':'P001'}]})
    changes=d.bind_sources(faithful.shots,long_passages,'source_quote',longer)
    assert faithful.shots[0].source_quote==long_passages['P001']
    assert [change['action'] for change in changes]==['source_quote_rebound']

    exact=d.Board.model_validate({'title':'t','scope_note':'s','shots':[{
        **{k:v for k,v in shot.items()},'source_quote':'女主看不清鬼怪','source_ref':'P001'}]})
    assert d.bind_sources(exact.shots,passages,'source_quote',TEXT)==[]
