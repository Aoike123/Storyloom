"""Persisted identity -> costume reference -> shot-reference dependencies."""
import os
import uuid
from fastapi import HTTPException
from PIL import Image
from .db import DATA, Record, Task, uid
from .reference_image_model import REFERENCE_IMAGE_MODEL, REFERENCE_IMAGE_STEPS
from .video_storage import identity as storage_identity, snapshot_asset, verify_file
from .video_files import media_url
from .asset_sheets import VERSION, style_prompt
from .skill_runtime import render_node


STITCHED_REFERENCE_VERSION='identity-costume-side-by-side-v1'


def _write_stitched_reference(left,right,destination):
    """Place two reviewed images on a neutral board; never generate or alter the sources."""
    with Image.open(left) as source:
        identity=source.convert('RGB')
    with Image.open(right) as source:
        costume=source.convert('RGB')
    panel_width=max(identity.width,costume.width)
    panel_height=max(identity.height,costume.height)
    gutter=max(8,min(32,max(panel_width,panel_height)//32))
    width=panel_width*2+gutter*3
    height=max(panel_height+gutter*2,(width+1)//2)  # Keep the model-safe ratio at or below 2:1.
    board=Image.new('RGB',(width,height),(224,226,224))
    top=(height-panel_height)//2
    board.paste(identity,(gutter+(panel_width-identity.width)//2,top+(panel_height-identity.height)//2))
    board.paste(costume,(gutter*2+panel_width+(panel_width-costume.width)//2,top+(panel_height-costume.height)//2))
    if max(board.size)>2560:
        board.thumbnail((2560,2560),Image.Resampling.LANCZOS)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.stem+'.'+uuid.uuid4().hex+'.tmp.png')
    try:
        board.save(temporary,format='PNG',compress_level=6)
        os.replace(temporary,destination)
    finally:
        temporary.unlink(missing_ok=True)


def stitch_character_costume_reference(db,identity_asset,costume_asset,name):
    """Create/reuse one deterministic, non-AI reference file for a reviewed identity+costume pair."""
    for asset in (identity_asset,costume_asset):
        if not asset or asset.kind!='asset' or asset.data.get('status')!='approved':
            raise HTTPException(409,'人物与服装图片尚未审核，不能制作拼接参考板。')
        validate_asset_origin(db,asset)
    snapshots=[snapshot_asset(db,asset) for asset in (identity_asset,costume_asset)]
    dependencies=[{'asset_revision_id':snapshot.id,**snapshot.data} for snapshot in snapshots]
    reference_id=storage_identity('characterref',[STITCHED_REFERENCE_VERSION,*[snapshot.id for snapshot in snapshots]])
    destination=DATA/'media'/'assets'/'reference-boards'/f'{reference_id}.png'
    if not destination.is_file():
        _write_stitched_reference(verify_file(snapshots[0].data['file']),verify_file(snapshots[1].data['file']),destination)
    media=media_url(destination)
    row=db.get(Record,reference_id)
    if row:
        validate_dependencies(db,row.data)
        if row.data.get('media')!=media or row.data.get('identity_asset_id')!=identity_asset.id or row.data.get('costume_asset_id')!=costume_asset.id:
            raise HTTPException(409,'人物服装拼接参考板记录与来源不一致。')
        return row
    description=(f'非 AI 生成的双栏参考板：左栏为「{identity_asset.data.get("name") or name}」身份图，'
                 f'右栏为「{costume_asset.data.get("name") or "服装"}」服装图；两栏属于同一角色。'
                 '生成镜头时保留左栏身份特征，并采用右栏完整服装；不要把两栏画成两个人。')
    row=Record(id=reference_id,kind='asset',data={
        'name':name+' · 人物服装拼接参考','type':'character','description':description,
        'asset_kind':'character_costume_reference','identity_asset_id':identity_asset.id,
        'costume_asset_id':costume_asset.id,'asset_dependencies':dependencies,
        'status':'approved','media':media,'derived_without_model':True,
        'layout':{'version':STITCHED_REFERENCE_VERSION,'identity_panel':'left','costume_panel':'right'},
    })
    db.add(row);db.flush()
    db.add(Record(id=uid('audit'),kind='audit',data={'target':row.id,'action':'identity_costume_reference_stitched',
        'source_asset_ids':[identity_asset.id,costume_asset.id],'model_call':False}))
    return row


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
            raise HTTPException(422,'每位入镜人物必须绑定自己的一张独立服装图；不要为三图上限省略服装，调用方会仅在超限时条件拼接，拼接后仍超限才拆镜。')
