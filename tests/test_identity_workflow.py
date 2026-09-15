import copy
import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from backend import asset_sheets as sheets, asset_workflow, creative as c, worker, preproduction as prep
from backend.db import DATA,Record,Session,Task
from test_creative import creative,drain,advance
from asset_spec_fixtures import character,costume,scene,style,design_response


def test_identity_costume_reference_is_a_local_side_by_side_stitch_without_model_task():
    left=DATA/'media'/'identity-red.png';right=DATA/'media'/'costume-blue.png'
    Image.new('RGB',(256,256),(220,10,10)).save(left)
    Image.new('RGB',(256,256),(10,20,220)).save(right)
    before=(left.read_bytes(),right.read_bytes())
    with Session.begin() as db:
        identity=Record(id='identity-red',kind='asset',data={'name':'人物','status':'approved','media':'/media/identity-red.png'})
        clothing=Record(id='costume-blue',kind='asset',data={'name':'服装','status':'approved','media':'/media/costume-blue.png'})
        db.add_all([identity,clothing]);db.flush()
        combined=asset_workflow.stitch_character_costume_reference(db,identity,clothing,'人物 · 蓝衣')
        combined_id=combined.id;media=combined.data['media']
    assert (left.read_bytes(),right.read_bytes())==before
    with Image.open(DATA/'media'/media.removeprefix('/media/')) as image:
        assert image.width/image.height<=2
        assert image.getpixel((20,image.height//2))==(220,10,10)
        assert image.getpixel((image.width-20,image.height//2))==(10,20,220)
    with Session() as db:
        combined=db.get(Record,combined_id)
        assert combined.data['derived_without_model'] is True
        assert len(combined.data['asset_dependencies'])==2
        assert not list(db.scalars(select(Task)))


@pytest.mark.parametrize('phrase',['因为她很穷所以穿旧衣','为了体现恐怖氛围','黑色或者白色短发','普通漫画风格，像主角一样','主观视角始终失焦','她拥抱小孩','Maybe a young woman'])
def test_character_attributes_reject_narrative_and_ambiguity(phrase):
    person=character();person['appearance']['hair_shape']=phrase
    with pytest.raises(ValidationError):sheets.CharacterSheet.model_validate(person)


def test_style_is_professional_parameters_and_person_has_no_clothing():
    bad=style();bad['medium']='统一漫画风，表现主角的误会'
    with pytest.raises(ValidationError):sheets.VisualStyle.model_validate(bad)
    bad=character();bad['wardrobe']=costume()['wardrobe']
    with pytest.raises(ValidationError):sheets.CharacterSheet.model_validate(bad)
    bad=costume();bad['appearance']=character()['appearance']
    with pytest.raises(ValidationError):sheets.CostumeSheet.model_validate(bad)
    prompt=sheets.style_prompt(style())
    assert '两阶硬边' in prompt and '#29363D 60%' in prompt
    assert '不得绘制成可见色卡' in prompt and '不覆盖对象固有色' in prompt
    assert all(term not in prompt for term in ('主角','服装','剧情','观众'))
    bad=character();bad['name']='思思（红裙版）'
    with pytest.raises(ValidationError):sheets.CharacterSheet.model_validate(bad)
    with pytest.raises(ValidationError):sheets.WardrobeScenePlan.model_validate({'items':[character(),scene()]})


def two_costume_design(system,payload,*args,**kwargs):
    if payload['schema']['title']=='CostumePlan':
        assert [p['character_id'] for p in payload['locked_characters']]==['C001']
        return {'costumes':[costume(),costume('W002','#EEEEEE')]},{}
    return design_response(system,payload,*args,**kwargs)


def test_real_workflow_keeps_two_costumes_on_one_frozen_identity(creative,monkeypatch):
    """一个身份可以有多套服装；服装与身份绑定后，同一镜头不能把两套服装当成两个角色。

    定装合成步骤已移除，因此这里不再生成定装图；绑定与去重规则仍需成立。
    """
    calls=[]
    def design(system,payload,*args,**kwargs):
        calls.append(payload['schema']['title'])
        return two_costume_design(system,payload,*args,**kwargs)
    monkeypatch.setattr(c,'chat_json',design)
    response=creative.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'悬疑','confirm_paid':True})
    drain()
    # Design runs once per specification, then prompt batches stay role-homogeneous: the single
    # character identity is one batch, and the two costumes plus the scene fill two more.
    assert calls==['StylePlan','CharacterPlan','CostumePlan','ScenePlan',
                   'AssetPromptBatch','AssetPromptBatch','AssetPromptBatch']
    with Session() as db:
        run=db.get(Record,'creative_pid');items=run.data['items']
        identity=next(i for i in items if i['role']=='character')
        identity_task=db.get(Task,identity['task_id'])
        identity_asset=identity_task.result['asset_id']
        assert len([i for i in items if i['role']=='character'])==1
        assert len([i for i in items if i['role']=='costume'])==2
        assert len(list(db.scalars(select(Task).where(Task.kind=='image'))))==4
        assert 'wardrobe' not in identity_task.payload['asset_spec']
    assert advance(creative,'assets_review').status_code==409
    drain()
    with Session.begin() as db:
        run=db.get(Record,'creative_pid')
        rows=[db.get(Record,db.get(Task,item['task_id']).result['asset_id']) for item in run.data['items']]
        c.approve_images(db,[row.id for row in rows])
        identity=next(row for row in rows if row.data.get('asset_kind')=='character_sheet')
        costumes=[row for row in rows if row.data.get('asset_kind')=='costume_sheet']
        scene=next(row for row in rows if row.data.get('asset_kind')=='scene_sheet')
    assert identity.id==identity_asset
    # 两套服装都绑定同一个人物身份：换装不会新增人物身份，两套服装可以并存。
    prep.save('pid',prep.Setup(expected_version=0,style='固定二维漫画画风与冷色光线',assets={
        identity.id:{'role':'character','name':'女主','notes':'固定短发与灰色服装'},
        costumes[0].id:{'role':'costume','name':'外套','notes':'深灰长外套','identity_asset_id':identity.id},
        costumes[1].id:{'role':'costume','name':'连衣裙','notes':'藏蓝连衣裙','identity_asset_id':identity.id},
        scene.id:{'role':'scene','name':'走廊','notes':'门在左侧，窗在右侧'}}))
    with Session() as db:
        assert set(db.get(Record,'prep_pid').data['assets'])=={identity.id,costumes[0].id,costumes[1].id,scene.id}
    # 同一镜头把同一身份的两个组合参考当作两个角色会被拒绝。
    editable={identity.id:{'role':'character','identity_asset_id':identity.id},
              costumes[0].id:{'role':'character','identity_asset_id':identity.id,'costume_asset_id':costumes[0].id},
              costumes[1].id:{'role':'character','identity_asset_id':identity.id,'costume_asset_id':costumes[1].id},
              scene.id:{'role':'scene'}}
    with pytest.raises(HTTPException,match='同一人物'):
        asset_workflow.validate_shot_identities([identity.id,costumes[0].id,costumes[1].id,scene.id],editable)
    with Session.begin() as db:db.get(Record,identity.id).version+=1
    with Session() as db:
        with pytest.raises(HTTPException,match='参考图已变化'):
            prep.snapshot(db,'pid')


def test_costume_edit_does_not_recreate_character_identity(creative,monkeypatch):
    monkeypatch.setattr(c,'chat_json',two_costume_design)
    creative.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'悬疑','confirm_paid':True});drain()
    with Session() as db:
        items=db.get(Record,'creative_pid').data['items']
        identity=copy.deepcopy(next(i for i in items if i['role']=='character'))
        clothing=next(i for i in items if i['role']=='costume')
    changed=costume();changed['wardrobe'][0]['color']='#203F8B'
    monkeypatch.setattr(c,'chat_json',lambda system,payload,*a,**k:({'items':[{'asset_index':0,'prompt':payload['assets'][0]['render_contract']}]} if payload['schema']['title']=='AssetPromptBatch' else changed,{}))
    response=creative.post('/api/creative/pid/feedback',json={'task_id':clothing['task_id'],'text':'裙子改成蓝色','confirm_paid':True})
    assert response.status_code==200;drain()
    with Session() as db:
        items=db.get(Record,'creative_pid').data['items']
        assert next(i for i in items if i['role']=='character')==identity
        updated=next(i for i in items if i.get('costume_id')=='W001')
        task=db.get(Task,updated['task_id'])
        assert task.payload['asset_spec']['character_ref']=='C001'
        assert '#203F8B' in task.payload['prompt']
        assert len([t for t in db.scalars(select(Task).where(Task.kind=='image')) if t.payload.get('asset_kind')=='character_sheet'])==1


# 已废弃线路：定装合成（v1–v5）。下面的用例验证"身份图变化后旧的定装任务不会重新提交"，随
# queue_fittings 一起停用；当前线路不再生成定装图，版本失效规则由 prep.snapshot 与
# prep.approve_references 承担（见上面的版本断言）。
# def test_changed_identity_stops_fitting_before_paid_request(creative,monkeypatch):
#     creative.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'悬疑','confirm_paid':True});drain()
#     assert advance(creative,'assets_review').status_code==200
#     with Session.begin() as db:
#         look=db.get(Record,'creative_pid').data['looks'][0]
#         db.get(Record,look['identity_asset_id']).version+=1
#     monkeypatch.setattr(worker,'generate_image',lambda *a,**k:pytest.fail('A stale identity must not generate a new fitting'))
#     monkeypatch.setattr(worker,'generate_from_references',lambda *a,**k:pytest.fail('A stale identity must not compose a fitting'))
#     assert worker.process_one('fitting-test')
#     with Session() as db:assert db.get(Task,look['task_id']).status=='needs_review'


def test_legacy_prompt_cannot_reenter_asset_generation(creative,monkeypatch):
    result=creative.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'悬疑','confirm_paid':True})
    with Session.begin() as db:
        task=db.get(Task,result.json()['id']);task.payload={**task.payload,'asset_schema':'asset-sheets-v2'}
        run=db.get(Record,'creative_pid');run.data={**run.data,'raw_design':{'visual_language':'主角打斗时的漫画风格','items':[]}}
    monkeypatch.setattr(c,'chat_json',lambda *a,**k:pytest.fail('Legacy narrative output must not be replayed'))
    worker.process_one('legacy-test')
    with Session() as db:
        assert db.get(Task,result.json()['id']).status=='needs_review'
        assert not list(db.scalars(select(Task).where(Task.kind=='image')))


# 已废弃线路：定装的复用规则（身份与服装都没变时沿用原定装任务，只有服装变化时重建该套定装）。
# def test_fitting_graph_reuses_unchanged_pair_and_rebuilds_only_changed_costume(creative,monkeypatch):
#     monkeypatch.setattr(c,'chat_json',two_costume_design)
#     creative.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'悬疑','confirm_paid':True});drain()
#     advance(creative,'assets_review');drain()
#     with Session.begin() as db:
#         run=db.get(Record,'creative_pid');before=copy.deepcopy(run.data['looks'])
#         assets={i['task_id']:db.get(Record,db.get(Task,i['task_id']).result['asset_id']) for i in run.data['items']}
#         asset_workflow.queue_fittings(db,run,'pid',assets)
#         assert run.data['looks']==before
#     with Session.begin() as db:
#         run=db.get(Record,'creative_pid')
#         costume_asset=db.get(Record,before[0]['costume_asset_id']);costume_asset.version+=1
#         assets={i['task_id']:db.get(Record,db.get(Task,i['task_id']).result['asset_id']) for i in run.data['items']}
#         asset_workflow.queue_fittings(db,run,'pid',assets)
#         after=run.data['looks']
#         assert after[0]['task_id']!=before[0]['task_id']
#         assert after[1]['task_id']==before[1]['task_id']
#         assert after[0]['identity_asset_id']==before[0]['identity_asset_id']
#         task=db.get(Task,after[0]['task_id'])
#         assert task.payload['revision_of']==before[0]['task_id']
#         assert len([t for t in db.scalars(select(Task).where(Task.kind=='image')) if t.payload.get('asset_kind')=='character_sheet'])==1
