import json
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend import asset_sheets as sheets, authors, creative as c, image_provider, worker
from backend.db import Record, Session, Task, task_dict
from test_creative import creative, drain


from asset_spec_fixtures import character, costume, scene, style


def creature(name='孙悟空',species='花果山石猴',body_plan='拟人双足',costume_mode='required'):
    if body_plan=='兽首人身':
        body_nature='人类身体';body_kind='人类';stance='双足直立'
    elif body_plan=='龙形':
        body_nature='龙类身体';body_kind='龙类';stance='四足着地'
    else:
        body_nature='兽类身体';body_kind='兽类';stance='双足直立'
    return {'role':'character','character_id':'C002','name':name,'source_ref':'P001','facts':'原文明确出现该神话角色',
        'costume_mode':costume_mode,'appearance':{
            'body_plan':body_plan,'body_nature':body_nature,'species':species,'life_stage':'成年体',
            'presentation':'男性外观','height_cm':175,'stance':stance,
            'head':{'nature':'兽类' if body_plan!='龙形' else '龙类','identity':species+'头部','structure':'前突短口鼻与圆耳固定头部结构',
                    'surface':'金棕色短毛仅覆盖头面与耳后','color':'#A66B32'},
            'torso':{'nature':body_kind,'identity':'成年男性人类躯干' if body_kind=='人类' else species+'躯干','structure':'宽肩躯干与固定胸腹比例',
                     'surface':'人类皮肤覆盖颈部以下躯干' if body_kind=='人类' else '金棕色短毛覆盖躯干','color':'#B88255'},
            'arms_hands':{'nature':body_kind,'identity':'成年男性人类双臂与五指手' if body_kind=='人类' else species+'前肢','structure':'两条对称手臂与五指手掌',
                          'surface':'人类皮肤覆盖双臂与双手' if body_kind=='人类' else '金棕色短毛覆盖双臂','color':'#B88255'},
            'legs_feet':{'nature':body_kind,'identity':'成年男性人类双腿与双足' if body_kind=='人类' else species+'后肢','structure':'两条对称长腿与完整双足',
                         'surface':'人类皮肤覆盖双腿与双足' if body_kind=='人类' else '金棕色短毛覆盖双腿','color':'#B88255'},
            'junction':'兽类后颈在肩线上连接颈部以下躯干',
            'eye_shape':'双眼圆形金色虹膜与深色圆瞳','eye_color':'#D9A62E',
            'features':['一条金棕色长尾']}}


def test_sheet_prompt_excludes_story_and_model_written_composition():
    data={**character(),'facts':'NARRATIVE_SENTINEL：她挥拳击飞怪物，观众弹幕尖叫。',
          'adaptation_notes':'NOTES_SENTINEL：原文未提供胸针信息。'}
    item=sheets.CharacterSheet.model_validate(data)
    prompt=sheets.compose_prompt(style(),item)
    assert all(marker not in prompt for marker in ('NARRATIVE_SENTINEL','NOTES_SENTINEL','PROMPT_SENTINEL'))
    assert '正面、左侧面、背面' in prompt and '同一脸型' in prompt
    assert item.appearance.hair_shape in prompt and 'wardrobe' not in item.model_dump()
    assert '圆形胸针' not in prompt and '浅灰纯色背景' in prompt


@pytest.mark.parametrize(('name','species','body_plan'),[
    ('孙悟空','花果山石猴','拟人双足'),
    ('牛头人','牛首类人生物','兽首人身'),
    ('应龙','有翼应龙','龙形'),
])
def test_character_sheet_preserves_nonhuman_species(name,species,body_plan):
    data=creature(name,species,body_plan,'none' if body_plan=='龙形' else 'required')
    item=sheets.CharacterSheet.model_validate(data)
    prompt=sheets.compose_prompt(style(),item)
    assert species in prompt and body_plan in prompt
    assert '不得改成人类' in prompt and '物种与构型' in prompt
    assert ('不添加人类服装' in prompt)==(item.costume_mode=='none')


def test_unclothed_mythical_creature_does_not_require_fake_costume():
    dragon=creature('应龙','有翼应龙','龙形','none')
    plan=sheets.AssetSheetPlan.model_validate({'visual_style':style(),'items':[dragon,scene()]})
    assert [item.role for item in plan.items]==['character','scene']


def test_pig_head_human_body_is_structurally_locked_by_region():
    pig=creature('猪八戒','猪首类人神怪','兽首人身')
    item=sheets.CharacterSheet.model_validate(pig)
    prompt=sheets.compose_prompt(style(),item)
    assert '头身分区硬锁：头部=兽类；颈部以下=人类身体' in prompt
    assert '身份=成年男性人类双臂与五指手' in prompt and '身份=成年男性人类双腿与双足' in prompt
    assert '兽类特征止于头颈分界' in prompt and '不得生成完整兽身' in prompt
    pig['appearance']['body_nature']='兽类身体'
    with pytest.raises(ValidationError):sheets.CharacterSheet.model_validate(pig)


def test_demon_body_cannot_silently_fall_back_to_a_human_body():
    demon=creature('沙悟净','流沙河妖怪','拟人双足')
    demon['appearance']['body_nature']='妖异身体'
    for key,label in {'head':'头部','torso':'躯干','arms_hands':'臂手','legs_feet':'腿足'}.items():
        demon['appearance'][key]['nature']='妖异类'
        demon['appearance'][key]['identity']='沙悟净妖异'+label
        demon['appearance'][key]['surface']='青褐色粗厚妖异皮肤'
    item=sheets.CharacterSheet.model_validate(demon)
    prompt=sheets.compose_prompt(style(),item)
    assert '颈部以下=妖异身体' in prompt and '躯干：类别=妖异类' in prompt
    demon['appearance']['torso']['nature']='人类'
    with pytest.raises(ValidationError):sheets.CharacterSheet.model_validate(demon)


def test_plain_realism_cannot_be_translated_to_3d():
    photographic={**style(),'medium':'写实影视摄影','linework':'无轮廓线、以色块和明暗区分形体','shading':'物理材质明暗'}
    assert sheets.validate_style_intent(photographic,'真人写实电影感').medium=='写实影视摄影'
    assert sheets.validate_style_intent(photographic,'写实，不要使用 3D').medium=='写实影视摄影'
    wrong={**style(),'medium':'写实三维渲染'}
    with pytest.raises(ValueError,match='不等于三维'):sheets.validate_style_intent(wrong,'写实')
    assert sheets.validate_style_intent(wrong,'写实 3D').medium=='写实三维渲染'


def test_storyboard_and_virtual_overlay_do_not_satisfy_asset_schema():
    old={'name':'弹幕面板','role':'scene','source_ref':'P001','facts':'观众正在评论',
         'design':'高清直播视窗，表现人物打斗','prompt':'三格漫画加对话框'}
    with pytest.raises(ValidationError):
        sheets.AssetSheetPlan.model_validate({'visual_language':'漫画式动作线和连续故事画面','items':[character(),old]})


def test_new_design_creates_role_specific_sheet_tasks(creative):
    result=creative.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'悬疑','confirm_paid':True})
    assert result.status_code==200
    assert worker.process_one('sheet-test')
    with Session() as db:
        design=db.get(Task,result.json()['id'])
        assert design.status=='completed' and design.payload['asset_schema']==sheets.VERSION
        tasks=list(db.scalars(select(Task).where(Task.kind=='image')))
        assert len(tasks)==3
        by_kind={t.payload['asset_kind']:t.payload['prompt'] for t in tasks}
        assert '正面、左侧面、背面' in by_kind['character_sheet']
        assert '无人全景' in by_kind['scene_sheet'] and '俯视布局图' in by_kind['scene_sheet']
        assert all('原著事实：' not in prompt and '剧情氛围：' not in prompt for prompt in by_kind.values())


def test_redesign_retains_old_assets_and_changes_current_outputs(creative):
    creative.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'温馨','confirm_paid':True})
    drain()
    with Session.begin() as db:
        run=db.get(Record,'creative_pid')
        old_ids=[item['task_id'] for item in run.data['items']]
        db.add(Record(id='prep_pid',kind='preproduction',data={'versions':{},'assets':{}}))
        db.add(Record(id='prep_gate_pid',kind='preproduction_gate',data={'stamp':'old'}))
        db.add(Record(id='sheet-work',kind='author_project',data={
            'director_id':'pid','source_id':'source','stage':'assets_review','art':'手绘漫画','tone':'温馨'}))
    path='/api/author/projects/sheet-work/redesign'
    assert creative.post(path,json={'art':'手绘漫画','tone':'温馨'}).status_code==422
    assert creative.post(path,json={'art':'手绘漫画','tone':'温馨','confirm_paid':True}).status_code==200
    assert creative.post(path,json={'art':'手绘漫画','tone':'温馨','confirm_paid':True}).status_code==409
    drain()
    with Session.begin() as db:
        for task in db.scalars(select(Task).where(Task.kind=='author_flow',Task.status=='waiting')):task.lease=0
    drain()
    work=creative.get('/api/author/projects/sheet-work').json()
    assert work['stage']=='assets_review'
    assert len(work['outputs'])==3 and not {o['id'] for o in work['outputs']}.intersection(old_ids)
    assert [item['task_id'] for item in work['creative']['design_history'][0]['items']]==old_ids
    with Session() as db:
        assert db.get(Record,'prep_pid').data.get('invalidated_at')
        assert db.get(Record,'prep_gate_pid').data.get('invalidated_at')
        for tid in old_ids:
            task=db.get(Task,tid)
            assert task.status=='completed' and db.get(Record,task.result['asset_id']) is not None
        assert len(list(db.scalars(select(Task).where(Task.kind=='image'))))==6


def test_request_prompt_snapshot_survives_image_completion_and_is_exposed(client,monkeypatch):
    sent=[]
    prompt='角色三视图\n同一定装，正面、侧面、背面。'
    for key,value in {'IMAGE_PROVIDER':'siliconflow','IMAGE_MODEL':'test-image-model',
                      'IMAGE_ENDPOINT':'https://images.example.test/generate','IMAGE_API_KEY':'never-expose-this-key'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setattr(image_provider,'reserve_call',lambda *a,**k:None)
    def post(url,**kwargs):
        sent.append(kwargs['json'])
        return httpx.Response(200,json={'images':[{'url':'https://images.example.test/result.png'}]})
    monkeypatch.setattr(image_provider.httpx,'post',post)
    monkeypatch.setattr(worker,'save_image',lambda *a:'/media/sheet.png')
    with Session.begin() as db:
        db.add(Task(id='sheet-image',kind='image',payload={'mode':'live','prompt':prompt,'title':'主角三视图','asset_kind':'character_sheet'}))
    with Session() as db:
        assert task_dict(db.get(Task,'sheet-image'))['generation']['source']=='task'
    assert worker.process_one('sheet-request-test')
    with Session.begin() as db:
        task=db.get(Task,'sheet-image')
        assert task.status=='completed'
        assert task.result['generation_request']['prompt']==sent[0]['prompt']==prompt
        task.payload={**task.payload,'prompt':'changed after submission'}
    with Session() as db:
        public=task_dict(db.get(Task,'sheet-image'))['generation']
    assert public['source']=='request' and public['prompt']==prompt
    assert public['model']==sent[0]['model']=='test-image-model'
    assert public['image_size']=='1024x1024' and public['asset_kind']=='character_sheet'
    assert 'never-expose-this-key' not in json.dumps(public)
    with Session() as db:
        assert authors.completed_outputs([db.get(Task,'sheet-image')])[0]['generation']==public
