"""Persisted identity -> costume fitting -> shot-reference dependencies."""
from fastapi import HTTPException
from .db import Record, Task, uid
from .reference_image_model import REFERENCE_IMAGE_MODEL, REFERENCE_IMAGE_STEPS
from .video_storage import snapshot_asset, verify_file
from .asset_sheets import VERSION, style_prompt
from .skill_runtime import render_node


def validate_asset_origin(db,asset):
    task=db.get(Task,asset.data.get('source_task',''))
    if not task:return
    for key in ('creative_id','director_id','preproduction_id','project_id'):
        project=db.get(Record,task.payload.get(key,''))
        if project and project.kind=='director' and project.data.get('superseded_by_run'):
            raise HTTPException(409,'该素材属于已失效的旧轮次，不能作为当前制作的参考。')


def validate_dependencies(db,payload):
    for dependency in payload.get('asset_dependencies',[]):
        asset=db.get(Record,dependency['asset_id'])
        if not asset or asset.version!=dependency['revision'] or asset.data.get('status')!='approved':
            raise HTTPException(409,'依赖的人物身份或服装版本已变化，请重新确认并生成定装。')
        validate_asset_origin(db,asset)
        if dependency.get('file'):verify_file(dependency['file'])


def queue_fittings(db,run,pid,assets):
    if run.data.get('asset_schema')!=VERSION:
        raise HTTPException(409,'旧版素材尚未分离人物身份与服装，请先重做本轮素材。')
    people={item['character_id']:(item,assets[item['task_id']]) for item in run.data['items'] if item['role']=='character'}
    looks=[]
    costumes=[item for item in run.data['items'] if item['role']=='costume']
    costumed={item['character_ref'] for item in costumes}
    for costume in costumes:
        person,identity=people[costume['character_ref']]
        clothing=assets[costume['task_id']]
        validate_asset_origin(db,identity);validate_asset_origin(db,clothing)
        snapshots=[snapshot_asset(db,a) for a in (identity,clothing)]
        dependencies=[{'asset_revision_id':s.id,**s.data} for s in snapshots]
        previous=next((look for look in run.data.get('looks',[]) if look['character_key']==person['character_id'] and look['costume_key']==costume['costume_id']),None)
        prior=db.get(Task,previous['task_id']) if previous else None
        if prior and [d['asset_revision_id'] for d in prior.payload.get('asset_dependencies',[])]==[d['asset_revision_id'] for d in dependencies]:
            if prior.status!='completed':raise HTTPException(409,'相同身份与服装的定装任务尚未完成，请先处理原任务，不能重复提交。')
            looks.append(previous)
            continue
        task=Task(id=uid('fitting'),kind='image',payload={
            'mode':'live','creative_id':pid,'asset_kind':'dressed_character','asset_role':'look','asset_schema':VERSION,
            'title':person['name']+' · 定装 '+costume['name'],
            'character_key':person['character_id'],'costume_key':costume['costume_id'],
            'identity_asset_id':identity.id,'costume_asset_id':clothing.id,'asset_dependencies':dependencies,
            'revision_of':prior.id if prior else None,
            'reference_media':[s.data['file']['media'] for s in snapshots],'image_model':REFERENCE_IMAGE_MODEL,
            'image_inference_steps':REFERENCE_IMAGE_STEPS,
            **render_node('fitting',{'style':style_prompt(run.data['visual_style'])})})
        db.add(task)
        looks.append({'task_id':task.id,'name':person['name']+' · '+costume['name'],
            'character_key':person['character_id'],'costume_key':costume['costume_id'],
            'identity_asset_id':identity.id,'costume_asset_id':clothing.id})
    for person,identity in people.values():
        if person['character_id'] in costumed:continue
        if person.get('costume_mode','required')=='required':
            raise HTTPException(409,'缺少绑定人物身份的独立服装。')
        validate_asset_origin(db,identity)
        looks.append({'task_id':person['task_id'],'name':person['name'],
            'character_key':person['character_id'],'costume_key':None,
            'identity_asset_id':identity.id,'costume_asset_id':None,'direct_identity':True})
    if not looks:raise HTTPException(409,'缺少可用的角色身份参考图。')
    run.data={**run.data,'looks':looks,'stage':'fittings_review'};run.version+=1


def validate_shot_identities(ids,assets):
    identities=[assets[aid].get('identity_asset_id') or aid for aid in ids if assets[aid]['role']=='character']
    if len(identities)!=len(set(identities)):
        raise HTTPException(422,'同一镜头不能把同一人物的不同服装当作两个角色，请只选一个定装结果。')
    characters={aid for aid in ids if assets[aid]['role']=='character'}
    costumes=[aid for aid in ids if assets[aid]['role']=='costume']
    for costume in costumes:
        if assets[costume].get('identity_asset_id') not in characters:
            raise HTTPException(422,'服装参考图必须与所属人物身份图在同一镜头中使用。')
    for character in characters:
        selected=[costume for costume in costumes if assets[costume].get('identity_asset_id')==character]
        if assets[character].get('requires_costume') and len(selected)!=1:
            raise HTTPException(422,'每位入镜人物必须绑定自己的一张独立服装图；超过参考图数量时请拆镜。')
