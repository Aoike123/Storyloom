"""Version-bound shot production and immutable reader releases."""
import time
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from .db import Session,Record,Task,uid,task_dict,record_dict
from .providers import paid_gate,settings
from .consistency import config,stamp,ready
from .video_storage import (generation_snapshot, create_use, use_entry, export_manifest, storage_view)

router=APIRouter(prefix='/api/production',tags=['production'])

def project(db,pid):
    row=db.get(Record,pid)
    if not row or row.kind!='director':raise HTTPException(404,'导演方案不存在。')
    return row

def matching(task,pid,version,shot_id=None,token=None):
    p=task.payload
    return p.get('consistency_stamp')==token and p.get('director_id')==pid and p.get('director_version')==version and (shot_id is None or p.get('shot_id')==shot_id)

REFERENCE_ROLE_USAGE={
    'character':'人物身份：保留物种、头面结构、脸型、发型、体表与体型；最终衣着以该角色的独立服装参考为准',
    'costume':'独立服装：该人物在整段中的最终衣着以此为准',
    'scene':'场景：空间结构、固定布局、材质与光线以此为准',
    'prop':'道具：形状、材质与位置以此为准',
}

def validate_references(db,task):
    """A finished video stays bound to the exact reviewed reference images it was made from."""
    frozen=task.payload.get('reference_assets') or {}
    pid=task.payload.get('director_id','')
    project_row=db.get(Record,pid);c=config(db,pid)
    if not frozen or not project_row or not c or project_row.version!=task.payload.get('director_version'):
        raise HTTPException(409,'视频对应的镜头参考图已变更，请使用最新审核版本重新制作。')
    from .consistency import shot_reference_ids
    if set(shot_reference_ids(db,c,task.payload['shot_id']))!=set(frozen):
        raise HTTPException(409,'该镜绑定的参考图已变更，请使用最新审核版本重新制作。')
    for aid,version in frozen.items():
        a=db.get(Record,aid)
        if not a or a.data.get('status')!='approved' or a.version!=version:
            raise HTTPException(409,'视频对应的镜头参考图已变更，请使用最新审核版本重新制作。')

def reviewed_references(db,c,sid):
    """Ordered, version-checked project references for one shot, including any stitched board."""
    from .consistency import shot_reference_ids
    from .preproduction import MAX_REFERENCE_IMAGES
    rows=[]
    for asset_id in shot_reference_ids(db,c,sid):
        a=db.get(Record,asset_id)
        if not a or a.kind!='asset' or a.data.get('status')!='approved':
            raise HTTPException(409,'镜头参考图尚未审核或已经失效，请重新确认基础参考图。')
        if c.data.get('asset_versions',{}).get(asset_id)!=a.version:
            raise HTTPException(409,'镜头参考图已变更，请重新保存并审核视觉设定。')
        rows.append(a)
    if not 1<=len(rows)<=MAX_REFERENCE_IMAGES:
        raise HTTPException(422,f'视频需绑定 1–{MAX_REFERENCE_IMAGES} 张已审核参考图片；超过上限请拆镜。')
    return rows

def reference_usage(db,pid,asset):
    """Describe a reviewed reference by its project role, never by layout or merge instructions."""
    if asset.data.get('asset_kind')=='character_costume_reference':
        return '人物与该角色的独立服装：输出为同一人物穿着这套服装'
    prep=db.get(Record,'prep_'+pid)
    spec=prep.data.get('assets',{}).get(asset.id,{}) if prep else {}
    role=spec.get('role') or asset.data.get('type')
    return REFERENCE_ROLE_USAGE.get(role,'人物、服装或场景外观参考')

@router.get('/{pid}')
def workspace(pid:str):
    with Session() as db:
        p=project(db,pid)
        c=config(db,pid);token=stamp(db,c) if c else 'unconfigured'
        tasks=list(db.scalars(select(Task).order_by(Task.created.desc())))
        shots=[]
        for shot in p.data.get('board',{}).get('shots',[]):
            jobs=[t for t in tasks if matching(t,pid,p.version,shot['id'],token)]
            video=next((t for t in jobs if t.kind=='video'),None)
            clip=db.get(Record,video.result.get('clip_id','')) if video else None
            shots.append({'shot':shot,'video_task':task_dict(video) if video else None,
                'clip':record_dict(clip) if clip else None,
                'storage':storage_view(db,clip) if clip else None})
        return {'project_id':pid,'version':p.version,'approved':p.data.get('status')=='approved','shots':shots}

class Command(BaseModel):
    version:int
    confirm_paid:bool=False

@router.post('/{pid}/shots/{sid}/video')
def generate_video(pid:str,sid:str,body:Command):
    cfg=settings()
    if not body.confirm_paid:raise HTTPException(422,'请确认视频生成费用。')
    refusal=paid_gate(cfg,'video')
    if refusal:raise HTTPException(422,refusal)
    if cfg.get('editable',{}).get('VIDEO_PROVIDER')!='minimax':raise HTTPException(422,'当前图片参考视频流程使用 MiniMax H3。')
    with Session.begin() as db:
        p=project(db,pid)
        if p.version!=body.version or p.data.get('status')!='approved':raise HTTPException(409,'请审核当前版本分镜。')
        shot=next((s for s in p.data['board']['shots'] if s['id']==sid),None)
        if not shot:raise HTTPException(404,'镜头不存在。')
        c,token,_=ready(db,pid,batch=True)
        rows=reviewed_references(db,c,sid)
        frozen={row.id:row.version for row in rows}
        jobs=[t for t in db.scalars(select(Task).order_by(Task.created.desc())) if matching(t,pid,p.version,sid,token)]
        existing=next((t for t in jobs if t.kind=='video' and t.payload.get('input_mode')=='reference_images' and t.payload.get('reference_assets')==frozen and t.status in ('queued','running','waiting','completed')),None)
        if existing:return task_dict(existing)
        prior=next((t for t in jobs if t.kind=='video'),None)
        from .skill_runtime import render_node
        payload={'mode':'live','input_mode':'reference_images','title':p.data['board']['title']+' '+sid,
            'consistency_stamp':token,'reference_assets':frozen,'director_id':pid,'director_version':p.version,
            'shot_id':sid,'generation_seconds':shot['generation_seconds'],'edit_seconds':shot['edit_seconds'],
            **({'revision_of':prior.id} if prior else {})}
        snapshot=generation_snapshot(db,payload,p,shot,reference_mode=True,references=rows)
        bindings=snapshot['asset_bindings']
        from .preproduction import MAX_REFERENCE_IMAGES
        if not 1<=len(bindings)<=MAX_REFERENCE_IMAGES or any(not binding.get('file') for binding in bindings):
            raise HTTPException(422,f'视频需绑定 1–{MAX_REFERENCE_IMAGES} 张有效参考图片。')
        references='\n'.join(f"参考图 {index+1}：{binding.get('name') or '项目参考图'}；用途：{reference_usage(db,pid,rows[index])}" for index,binding in enumerate(bindings))
        style=c.data.get('style')
        payload.update(render_node('video_render',{
            'style':f'统一视觉：{style}' if style else '统一视觉：沿用所附参考图的既有画风。',
            'composition':shot.get('reference_prompt') or shot.get('first_frame') or '',
            'motion':shot['motion_prompt'],'references':references}))
        payload['input_snapshot']={**snapshot,'prompt':payload['prompt']}
        payload['reference_media']=[binding['file']['media'] for binding in bindings]
        task=Task(id=uid('video'),kind='video',payload=payload)
        db.add(task);db.flush();return task_dict(task)

class Trim(BaseModel):
    version:int
    start:float=Field(ge=0,allow_inf_nan=False)
    end:float=Field(gt=0,allow_inf_nan=False)
    confirm_visual:bool=False

@router.post('/{pid}/shots/{sid}/approve-clip')
def approve_clip(pid:str,sid:str,body:Trim):
    if not body.confirm_visual:raise HTTPException(422,'请看过视频并确认选取区间。')
    with Session.begin() as db:
        p=project(db,pid)
        if p.version!=body.version or p.data.get('status')!='approved':raise HTTPException(409,'分镜版本已变化。')
        _,token,_=ready(db,pid,batch=True)
        jobs=[t for t in db.scalars(select(Task).order_by(Task.created.desc())) if t.kind=='video' and matching(t,pid,p.version,sid,token)]
        task=next(iter(jobs),None)
        if task:validate_references(db,task)
        clip=db.get(Record,task.result.get('clip_id','')) if task else None
        if not clip or not body.start<body.end<=clip.data['duration']:raise HTTPException(422,'视频不存在或选取区间超出素材。')
        if clip.data.get('locked'):raise HTTPException(409,'此片段已发布，请制作新的版本。')
        shot_ref=(task.payload.get('input_snapshot') or {}).get('shot') or {'story_id':p.data.get('source_id'),'director_id':pid,'shot_id':sid,'revision':p.version}
        use=create_use(db,clip,body.start,body.end,shot_ref)
        clip.data={**clip.data,'status':'approved','reader_trim':{'start':use.data['start'],'end':use.data['end']},
                   'selected_use_id':use.id,'visual_reviewed':True};clip.version+=1
        db.add(Record(id=uid('audit'),kind='audit',data={'target':clip.id,'action':'reader_clip_reviewed','start':body.start,'end':body.end}))
        return record_dict(clip)

class Publish(BaseModel):
    version:int
    confirm:bool=False

@router.post('/{pid}/publish')
def publish(pid:str,body:Publish):
    if not body.confirm:raise HTTPException(422,'请确认将审核完成的短片放入本地读者空间。')
    with Session.begin() as db:
        p=project(db,pid)
        if p.version!=body.version or p.data.get('status')!='approved':raise HTTPException(409,'方案版本尚未批准。')
        _,token,_=ready(db,pid,batch=True)
        rid=f'release_{pid}_{p.version}_{token[:12]}'
        previous=db.get(Record,rid)
        if previous:
            export_manifest(previous)
            return record_dict(previous)
        jobs=list(db.scalars(select(Task).order_by(Task.created.desc())))
        entries=[]
        for shot in p.data['board']['shots']:
            task=next((t for t in jobs if t.kind=='video' and matching(t,pid,p.version,shot['id'],token)),None)
            if task:validate_references(db,task)
            clip=db.get(Record,task.result.get('clip_id','')) if task else None
            if not clip or clip.data.get('status')!='approved' or not clip.data.get('visual_reviewed') or not clip.data.get('reader_trim'):
                raise HTTPException(422,f'{shot["id"]} 尚未完成视频审核与剪辑区间选择。')
            use=db.get(Record,clip.data.get('selected_use_id',''))
            if not use:
                trim=clip.data['reader_trim']
                shot_ref=(task.payload.get('input_snapshot') or {}).get('shot') or {'story_id':p.data.get('source_id'),'director_id':pid,'shot_id':shot['id'],'revision':p.version}
                use=create_use(db,clip,trim['start'],trim['end'],shot_ref)
                clip.data={**clip.data,'selected_use_id':use.id}
            if use.data['clip_id']!=clip.id or use.data['shot'].get('director_id')!=pid or use.data['shot'].get('shot_id')!=shot['id'] or use.data['shot'].get('revision')!=p.version:
                raise HTTPException(409,'选用记录与当前分镜版本不一致，请重新审片。')
            entries.append(use_entry(db,use,f'{rid}:{shot["id"]}:{len(entries)}'))
            clip.data={**clip.data,'locked':True}
        source=db.get(Record,p.data['source_id'])
        release=Record(id=rid,kind='reader_release',data={'schema_version':1,'manifest_revision':1,'status':'ready',
            'title':p.data['board']['title'],'author':source.data.get('author_name'),
            'source_title':source.data.get('title'),'source_id':source.id,'source_work_id':source.data.get('work_id'),'director_id':pid,'director_version':p.version,
            'description':p.data['treatment']['premise'],'scope':'短场景改编','entries':entries,'published_at':time.time()})
        db.add(release);db.flush()
        response=record_dict(release)
    export_manifest(release)
    return response

reader=APIRouter(prefix='/api/reader',tags=['reader'])

@reader.get('/stories')
def reader_stories():
    with Session() as db:
        return [{'id':r.id,**{k:r.data.get(k) for k in ('title','author','source_title','description','entries')}} for r in db.scalars(select(Record).where(Record.kind=='reader_release').order_by(Record.created.desc())) if not r.data.get('superseded_by_run')]

class Wish(BaseModel):
    release_id:str
    index:int=Field(ge=0)
    offset:float=Field(ge=0,allow_inf_nan=False)
    text:str=Field(min_length=2,max_length=2000)

@reader.post('/wishes')
def save_wish(body:Wish):
    with Session.begin() as db:
        release=db.get(Record,body.release_id)
        if not release or release.kind!='reader_release':raise HTTPException(404,'作品不存在。')
        if release.data.get('superseded_by_run'):raise HTTPException(409,'此作品版本已因上游重测失效，请刷新目录。')
        entries=release.data['entries']
        if body.index>=len(entries) or not entries[body.index]['start']<=body.offset<=entries[body.index]['end']:raise HTTPException(422,'暂停位置无效。')
        wish=Record(id=uid('wish'),kind='reader_wish',data={**body.model_dump(),'status':'saved','note':'已记录创意，尚未生成或替换后续视频。'})
        db.add(wish);db.flush();return record_dict(wish)
