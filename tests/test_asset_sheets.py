import json
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend import asset_sheets as sheets, authors, creative as c, image_provider, worker
from backend.db import Record, Session, Task, task_dict
from test_creative import creative, drain


from asset_spec_fixtures import character, costume, scene, style


def test_sheet_prompt_excludes_story_and_model_written_composition():
    data={**character(),'facts':'NARRATIVE_SENTINEL：她挥拳击飞怪物，观众弹幕尖叫。',
          'adaptation_notes':'NOTES_SENTINEL：原文未提供胸针信息。'}
    item=sheets.CharacterSheet.model_validate(data)
    prompt=sheets.compose_prompt(style(),item)
    assert all(marker not in prompt for marker in ('NARRATIVE_SENTINEL','NOTES_SENTINEL','PROMPT_SENTINEL'))
    assert '正面、左侧面、背面' in prompt and '同一脸型' in prompt
    assert item.appearance.hair_shape in prompt and 'wardrobe' not in item.model_dump()
    assert '圆形胸针' not in prompt and '浅灰纯色背景' in prompt


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
