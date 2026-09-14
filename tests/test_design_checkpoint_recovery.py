import copy
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from backend import asset_sheets as sheets, authors, creative as c, worker
from backend.db import Record,Session,Task
from backend.visual_specs import Dimensions,SceneSheet
from asset_spec_fixtures import character,costume,scene,style
from test_creative import creative,drain


def checkpoints(scene_count=3):
    people=[];outfits=[]
    for i in range(1,7):
        person=character();person.update(character_id=f'C{i:03}',name=f'人物{i}')
        people.append(person)
        outfit=costume(f'W{i:03}');outfit['character_ref']=person['character_id']
        outfits.append(outfit)
    extra=costume('W007','#EEEEEE');extra['character_ref']='C002';outfits.append(extra)
    locations=[]
    for i in range(scene_count):
        location=scene();location['name']=f'地点{i+1}'
        if i==0:
            location['space_type']='高层住宅外部入口'
            location['dimensions']={'width_m':24,'depth_m':18,'height_m':108}
        locations.append(location)
    return {'visual_style':style(),'characters':people},{'items':outfits+locations}


def test_valid_building_height_and_stage_capacity_do_not_reject_completed_outputs():
    for count in (3,5):
        identity,materials=checkpoints(count)
        people=sheets.IdentityPlan.model_validate(identity)
        assets=sheets.WardrobeScenePlan.model_validate(materials)
        combined=sheets.AssetSheetPlan.model_validate({'visual_style':people.visual_style.model_dump(),
            'items':[p.model_dump() for p in people.characters]+[a.model_dump() for a in assets.items]})
        assert len(combined.items)==13+count
        assert next(i for i in combined.items if i.role=='scene').dimensions.height_m==108


@pytest.mark.parametrize('value',[0,-1,float('inf'),float('nan'),True,'108'])
def test_dimensions_still_reject_invalid_numbers(value):
    with pytest.raises(ValidationError):Dimensions(width_m=24,depth_m=18,height_m=value)


def test_scene_layout_accepts_concrete_single_character_chinese_category():
    location=scene();location['layout'][0]['category']='床'
    assert SceneSheet.model_validate(location).layout[0].category=='床'
    location['layout'][0]['category']=' '
    with pytest.raises(ValidationError,match='静态物理属性不能为空'):
        SceneSheet.model_validate(location)


def prepare_stopped(client):
    response=client.post('/api/creative/pid/design',json={'art':'手绘漫画','tone':'悬疑','confirm_paid':True})
    assert response.status_code==200
    tid=response.json()['id'];identity,materials=checkpoints()
    with Session.begin() as db:
        task=db.get(Task,tid);task.status='needs_review';task.attempts=1
        task.payload={k:v for k,v in task.payload.items() if k!='skill_pipeline'}
        task.message='场景 dimensions.height_m 超过旧版100米限制'
        run=db.get(Record,'creative_pid')
        run.data={**run.data,'identity_plan':copy.deepcopy(identity),'wardrobe_scene_plan':copy.deepcopy(materials),'raw_design_task_id':tid}
        db.add(Task(id='stopped-flow',kind='author_flow',status='needs_review'))
        db.add(Record(id='checkpoint-work',kind='author_project',data={
            'director_id':'pid','source_id':'source','stage':'preparing','supervisor':'stopped-flow'}))
    return tid,identity,materials


def test_resume_combines_both_saved_stages_without_model_resubmission(creative,monkeypatch):
    tid,identity,materials=prepare_stopped(creative)
    monkeypatch.setattr(c,'chat_json',lambda *a,**k:pytest.fail('Completed design calls must not be repeated'))
    response=creative.post('/api/author/projects/checkpoint-work/resume')
    assert response.status_code==200,response.text
    assert worker.process_one('checkpoint-recovery')
    with Session() as db:
        run=db.get(Record,'creative_pid')
        assert run.data['identity_plan']==identity and run.data['wardrobe_scene_plan']==materials
        assert len(run.data['items'])==16 and len(run.data['raw_design']['items'])==16
        assert len(list(db.scalars(select(Task).where(Task.kind=='image'))))==16
        activity=db.get(Record,'activity_'+tid).data
        assert activity['phase']=='ready'
        assert len(activity['items'])==17 and all(i['text'] for i in activity['items'])
    drain()
    with Session.begin() as db:
        for task in db.scalars(select(Task).where(Task.kind=='author_flow',Task.status=='waiting')):task.lease=0
    drain()
    assert creative.get('/api/author/projects/checkpoint-work').json()['stage']=='assets_review'


def test_resume_rebuilds_combined_stage_from_saved_costume_and_scene_results(creative,monkeypatch):
    tid,identity,materials=prepare_stopped(creative)
    with Session.begin() as db:
        run=db.get(Record,'creative_pid');data=copy.deepcopy(run.data)
        data.pop('wardrobe_scene_plan')
        data['costume_plan']={'costumes':[item for item in materials['items'] if item['role']=='costume']}
        data['scene_plan']={'scenes':[item for item in materials['items'] if item['role']=='scene']}
        run.data=data
    monkeypatch.setattr(c,'chat_json',lambda *a,**k:pytest.fail('Saved design stages must not be resubmitted'))
    response=creative.post('/api/author/projects/checkpoint-work/resume')
    assert response.status_code==200,response.text
    assert worker.process_one('split-checkpoint-recovery')
    with Session() as db:
        run=db.get(Record,'creative_pid')
        assert len(run.data['raw_design']['items'])==16
        assert len(list(db.scalars(select(Task).where(Task.kind=='image'))))==16


@pytest.mark.parametrize('problem',['missing_stage','wrong_task','invalid_stage'])
def test_incomplete_or_invalid_checkpoints_cannot_trigger_paid_retries(creative,monkeypatch,problem):
    tid,_,_=prepare_stopped(creative)
    with Session.begin() as db:
        run=db.get(Record,'creative_pid');data=copy.deepcopy(run.data)
        if problem=='missing_stage':data.pop('wardrobe_scene_plan')
        elif problem=='wrong_task':data['raw_design_task_id']='another-task'
        else:data['wardrobe_scene_plan']['items'][7]['dimensions']['height_m']=-1
        run.data=data
    monkeypatch.setattr(c,'chat_json',lambda *a,**k:pytest.fail('Recovery must not resubmit a model'))
    assert creative.post('/api/author/projects/checkpoint-work/resume').status_code==409
    with Session() as db:
        assert db.get(Task,tid).status=='needs_review'
        assert not list(db.scalars(select(Task).where(Task.status=='queued')))


def test_supervisor_reports_the_child_validation_reason(creative):
    prepare_stopped(creative)
    with pytest.raises(HTTPException,match='dimensions.height_m') as error:
        authors.flow('unused',{'work_id':'checkpoint-work','phase':'preparing'})
    assert '流程阶段不匹配' not in str(error.value.detail)
