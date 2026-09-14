import copy
import hashlib
import json
from pathlib import Path
import pytest
from sqlalchemy import select

from backend import skill_runtime as skills,creative as c,director as d,worker
from backend.db import Record,Session,Task,task_dict
from backend.providers import ProviderError,ModelOutputError
from backend.asset_sheets import StylePlan
from backend.workflows import WORKFLOWS
from test_creative import creative
from test_director import board,TEXT
from asset_spec_fixtures import style,design_response


def test_every_production_node_has_a_real_versioned_skill_binding(client):
    nodes={n['node']:n for n in client.get('/api/node-skills').json()['nodes']}
    for module in WORKFLOWS['author-brainstorm-v3']['modules']:
        binding=nodes[module['skill_node']]
        assert binding['sources'] and len(binding['sha256'])==64
        assert all(len(source['commit'])==40 for source in binding['sources'])
        assert client.get('/api/node-skills/'+binding['node']).json()['effective_instructions']
    assert nodes['film_review']['mode']=='human' and nodes['asset_review']['checklist']
    assert client.get('/api/node-skills/unknown').status_code==404


def test_style_choice_uses_cinematic_language_before_asset_rendering_style():
    cinematic=skills.snapshot('style_options')
    rendering=skills.snapshot('style_spec')
    assert cinematic['title']=='电影视觉风格提案'
    assert [source['name'] for source in cinematic['sources']]==['director-visual-language']
    assert 'framing scale' in cinematic['system'] and 'lighting logic' in cinematic['system']
    assert rendering['title']=='基础美术渲染参数'
    assert [source['name'] for source in rendering['sources']]==['prompt-images']


def test_missing_binding_and_out_of_scope_story_input_never_call_model():
    calls=[]
    chat=lambda *a,**k:calls.append(a)
    with pytest.raises(ProviderError):skills.call_node(chat,'missing',{},'none')
    with pytest.raises(ProviderError,match='越界'):
        skills.call_node(chat,'style_spec',{'art_preference':'墨线','source':'a story must not enter the style node','schema':StylePlan.model_json_schema()},'none')
    assert calls==[]


def test_node_pins_skill_and_reuses_only_identical_completed_requests(monkeypatch):
    with Session.begin() as db:db.add(Task(id='node-task',kind='art_design',status='running',owner='tester'))
    calls=[]
    def chat(system,payload,*args,**kwargs):calls.append(system);return {'visual_style':style()},{}
    payload={'art_preference':'墨线绘制','schema':StylePlan.model_json_schema()}
    first=skills.call_node(chat,'style_spec',payload,'node-task')
    with Session() as db:
        pin=db.get(Task,'node-task').payload['node_skill_pins']['style_spec']
        assert pin['sha256']==hashlib.sha256(calls[0].encode()).hexdigest()
    monkeypatch.setattr(skills,'snapshot',lambda *a:pytest.fail('A running task must use its pinned skill'))
    assert skills.call_node(chat,'style_spec',payload,'node-task')==first
    skills.call_node(chat,'style_spec',{**payload,'art_preference':'透明水彩'},'node-task')
    assert len(calls)==2 and calls[0]==calls[1]
    with Session() as db:
        trace=task_dict(db.get(Task,'node-task'))['skill_calls']
        assert [row['status'] for row in trace]==['completed','reused','completed']


def test_invalid_structured_output_is_retried_three_times_before_success():
    with Session.begin() as db:db.add(Task(id='retry-node',kind='art_design',status='running',owner='tester'))
    calls=[]
    def chat(system,payload,*args,**kwargs):
        calls.append(system)
        return ({'visual_style':style()} if len(calls)==4 else {}),{}
    result,_=skills.call_node(chat,'style_spec',{'art_preference':'墨线绘制','schema':StylePlan.model_json_schema()},
        'retry-node',validator=StylePlan.model_validate)
    assert result.visual_style.medium and len(calls)==4
    assert '上一次输出校验结果' not in calls[0] and all('上一次输出校验结果' in value for value in calls[1:])
    with Session() as db:
        task=db.get(Task,'retry-node')
        assert len(task.result['model_output_errors'])==3
        assert [row['status'] for row in task_dict(task)['skill_calls']]==['invalid','invalid','invalid','completed']


def test_invalid_structured_output_reports_only_after_three_retries():
    calls=[]
    def chat(*args,**kwargs):
        calls.append(1);raise ModelOutputError('不是完整 JSON')
    with pytest.raises(ModelOutputError,match='3 次重试'):
        skills.call_node(chat,'style_spec',{'art_preference':'墨线绘制','schema':StylePlan.model_json_schema()},'none',
            validator=StylePlan.model_validate)
    assert len(calls)==4


def test_uncertain_provider_error_is_not_resubmitted():
    calls=[]
    def chat(*args,**kwargs):calls.append(1);raise ProviderError('请求结果不确定')
    with pytest.raises(ProviderError,match='不确定'):
        skills.call_node(chat,'style_spec',{'art_preference':'墨线绘制','schema':StylePlan.model_json_schema()},'none',
            validator=StylePlan.model_validate)
    assert len(calls)==1


def test_stopped_task_cannot_invoke_a_skill():
    with Session.begin() as db:db.add(Task(id='stopped-node',kind='art_design',status='cancelled'))
    with pytest.raises(ProviderError,match='未运行'):
        skills.call_node(lambda *a:pytest.fail('Cancelled task called a model'),'style_spec',{},'stopped-node')


def test_changed_upstream_source_is_rejected(tmp_path,monkeypatch):
    import shutil
    root=tmp_path/'node_skills';shutil.copytree(skills.ROOT,root)
    target=root/'vendor/replicate/skills/prompt-images/SKILL.md'
    target.write_text(target.read_text(encoding='utf-8')+'\nchanged',encoding='utf-8')
    monkeypatch.setattr(skills,'ROOT',root)
    with pytest.raises(ProviderError,match='发生变化'):skills.snapshot('style_spec')


def test_prompt_writer_output_is_the_image_request_and_never_receives_story(creative,monkeypatch):
    seen=[]
    def chat(system,payload,*args,**kwargs):
        seen.append(payload['schema']['title'])
        value,usage=design_response(system,payload,*args,**kwargs)
        if payload['schema']['title']=='AssetPromptBatch':
            assert set(payload)=={'assets','target','schema'}
            assert all(set(asset)=={'asset_index','role','render_contract'} for asset in payload['assets'])
            for item in value['items']:item['prompt']='专业提示词成品：'+item['prompt']
        return value,usage
    monkeypatch.setattr(c,'chat_json',chat)
    task=creative.post('/api/creative/pid/design',json={'art':'墨线漫画','tone':'悬疑','confirm_paid':True}).json()
    assert worker.process_one('node-tester')
    with Session() as db:
        assert db.get(Task,task['id']).status=='completed'
        images=list(db.scalars(select(Task).where(Task.kind=='image')))
        assert len(images)==3
        assert all(t.payload['prompt'].startswith('专业提示词成品：') for t in images)
        assert all(t.payload['node_skill']['node']=='asset_prompts' for t in images)
    assert seen[:4]==['StylePlan','CharacterPlan','CostumePlan','ScenePlan']


def test_incomplete_prompt_batch_uses_validated_contract_for_only_the_missing_asset(creative,monkeypatch):
    def chat(system,payload,*args,**kwargs):
        if payload['schema']['title']=='AssetPromptBatch':
            asset=payload['assets'][0]
            return {'items':[{'asset_index':asset['asset_index'],'prompt':'专业提示词成品：'+asset['render_contract']}]},{}
        return design_response(system,payload,*args,**kwargs)
    monkeypatch.setattr(c,'chat_json',chat)
    task=creative.post('/api/creative/pid/design',json={'art':'墨线漫画','tone':'悬疑','confirm_paid':True}).json()
    assert worker.process_one('node-tester')
    with Session() as db:
        parent=db.get(Task,task['id'])
        assert parent.status=='completed' and parent.result['prompt_fallbacks']==[0,1]
        run=db.get(Record,'creative_pid');assert run.data['raw_design']
        images=list(db.scalars(select(Task).where(Task.kind=='image')))
        assert len(images)==3
        by_title={item['name']:item for item in run.data['items']}
        for image in images:
            saved=by_title[image.payload['title']]
            if image.payload['prompt_source']=='validated_render_contract':
                assert image.payload['prompt']==saved['prompt']
                assert 'node_skill' not in image.payload
            else:
                assert image.payload['prompt'].startswith('专业提示词成品：')
                assert image.payload['node_skill']['node']=='asset_prompts'


def test_unparseable_prompt_output_falls_back_without_blocking_images(creative,monkeypatch):
    prompt_calls=0
    def chat(system,payload,*args,**kwargs):
        nonlocal prompt_calls
        if payload['schema']['title']=='AssetPromptBatch':
            prompt_calls+=1
            raise ModelOutputError('语言模型未返回符合约定的 JSON。')
        return design_response(system,payload,*args,**kwargs)
    monkeypatch.setattr(c,'chat_json',chat)
    task=creative.post('/api/creative/pid/design',json={'art':'墨线漫画','tone':'悬疑','confirm_paid':True}).json()
    assert worker.process_one('node-tester')
    with Session() as db:
        parent=db.get(Task,task['id'])
        assert parent.status=='completed' and parent.result['prompt_fallbacks']==[0,1,2]
        images=list(db.scalars(select(Task).where(Task.kind=='image')))
        assert len(images)==3 and all(t.payload['prompt_source']=='validated_render_contract' for t in images)
        run=db.get(Record,'creative_pid')
        assert all('3 次重试' in batch['fallback_reason'] for batch in run.data['image_prompt_batches'].values())
    assert prompt_calls==8


def test_shot_prompt_compiler_cannot_change_story_or_asset_bindings(monkeypatch):
    original=board();seen=[];saved={}
    treatment={key:'明确的阐述依据' for key in ('premise','dramatic_question','protagonist_goal','excerpt_scope','visual_strategy','information_strategy')}
    treatment.update(rules=[{'rule':'原文规则','quote':TEXT,'consequence':'动作后果'}],boundaries=['片段范围'])
    def chat(system,payload,*args,**kwargs):
        kind=payload['schema']['title'];seen.append(kind)
        if kind=='Board':return copy.deepcopy(original),{}
        if kind=='ShotPromptBatch':return {'shots':[{'id':s['id'],'reference_prompt':'专业静态镜头参考图提示词','motion_prompt':'专业单镜运动提示词'} for s in original['shots']]},{}
        return {'approved':True,'issues':[],'continuity':'通过','dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'},{}
    monkeypatch.setattr(d,'chat_json',chat)
    result=d.run_director({'source':{'content':TEXT},'brief':'短场景','stage':'board','treatment':treatment,'professional_prompts':True},'direct-node',lambda key,value,progress:saved.update({key:value}))
    assert seen==['Board','ShotPromptBatch','Review'] and result['approved']
    for before,after in zip(d.Board.model_validate(original).model_dump()['shots'],saved['board']['shots']):
        assert {k:v for k,v in before.items() if k not in ('reference_prompt','motion_prompt')}=={k:v for k,v in after.items() if k not in ('reference_prompt','motion_prompt')}
        assert after['reference_prompt']=='专业静态镜头参考图提示词'
    assert saved['board_plan']
