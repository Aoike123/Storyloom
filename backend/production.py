"""Version-bound shot production and immutable reader releases."""
import time
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from .db import Session,Record,Task,uid,task_dict,record_dict
from .providers import settings
from .image_provider import local_frame_data
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

def validate_frame(db,task):
    a=db.get(Record,task.payload.get('asset_id',''))
    latest=next((t for t in db.scalars(select(Task).where(Task.kind=='image').order_by(Task.created.desc())) if matching(t,task.payload['director_id'],task.payload['director_version'],task.payload['shot_id'],task.payload.get('consistency_stamp'))),None)
    if not a or a.data.get('status')!='approved' or a.version!=task.payload.get('asset_version') or not latest or latest.result.get('asset_id')!=a.id:
        raise HTTPException(409,'视频对应的镜头参考图已变更，请使用最新审核版本重新制作。')

@router.get('/{pid}')
def workspace(pid:str):
    with Session() as db:
        p=project(db,pid)
        c=config(db,pid);token=stamp(db,c) if c else 'unconfigured'
        tasks=list(db.scalars(select(Task).order_by(Task.created.desc())))
        shots=[]
        for shot in p.data.get('board',{}).get('shots',[]):
            jobs=[t for t in tasks if matching(t,pid,p.version,shot['id'],token)]
            image=next((t for t in jobs if t.kind=='image'),None)
            video=next((t for t in jobs if t.kind=='video' and image and t.payload.get('asset_id')==image.result.get('asset_id')),None)
            asset=db.get(Record,image.result.get('asset_id','')) if image else None
            clip=db.get(Record,video.result.get('clip_id','')) if video else None
            shots.append({'shot':shot,'image_task':task_dict(image) if image else None,'video_task':task_dict(video) if video else None,
                'asset':record_dict(asset) if asset else None,'clip':record_dict(clip) if clip else None,
                'storage':storage_view(db,clip) if clip else None})
        return {'project_id':pid,'version':p.version,'approved':p.data.get('status')=='approved','shots':shots}

class Command(BaseModel):
    version:int
    confirm_paid:bool=False

@router.post('/{pid}/shots/{sid}/video')
def generate_video(pid:str,sid:str,body:Command):
    cfg=settings()
    if not body.confirm_paid or not cfg['paid_enabled'] or not cfg['video_configured']:raise HTTPException(422,'请确认视频生成费用，并配置视频模型。')
    if cfg.get('editable',{}).get('VIDEO_PROVIDER')!='minimax':raise HTTPException(422,'当前图片参考视频流程使用 MiniMax H3。')
    with Session.begin() as db:
        p=project(db,pid)
        if p.version!=body.version or p.data.get('status')!='approved':raise HTTPException(409,'请审核当前版本分镜。')
        shot=next((s for s in p.data['board']['shots'] if s['id']==sid),None)
        if not shot:raise HTTPException(404,'镜头不存在。')
        _,token,_=ready(db,pid,batch=True)
        jobs=[t for t in db.scalars(select(Task).order_by(Task.created.desc())) if matching(t,pid,p.version,sid,token)]
        image=next((t for t in jobs if t.kind=='image'),None)
        asset=db.get(Record,image.result.get('asset_id','')) if image else None
        if not asset or asset.data.get('status')!='approved':raise HTTPException(422,'先生成并审核该镜参考图。')
        existing=next((t for t in jobs if t.kind=='video' and t.payload.get('input_mode')=='reference_images' and t.payload.get('asset_id')==asset.id and t.payload.get('asset_version')==asset.version and t.status in ('queued','running','waiting','completed')),None)
        if existing:return task_dict(existing)
        prior=next((t for t in jobs if t.kind=='video'),None)
        try:local_frame_data(asset.data.get('media'))
        except Exception:raise HTTPException(422,'镜头参考图文件无效，请检查素材。') from None
        from .skill_runtime import render_node
        payload={'mode':'live','input_mode':'reference_images','title':p.data['board']['title']+' '+sid,
            'consistency_stamp':token,'asset_id':asset.id,'asset_version':asset.version,'director_id':pid,'director_version':p.version,
            'shot_id':sid,'generation_seconds':shot['generation_seconds'],'edit_seconds':shot['edit_seconds'],
            **({'revision_of':prior.id} if prior else {})}
        snapshot=generation_snapshot(db,payload,p,shot,asset,reference_mode=True)
        bindings=snapshot['asset_bindings']
        if not 1<=len(bindings)<=9 or any(not binding.get('file') for binding in bindings):
            raise HTTPException(422,'视频需绑定 1–9 张有效参考图片。')
        references='\n'.join(f"参考图 {index+1}：{binding.get('name') or '镜头参考'}；用途：{'镜头构图与画风参考' if binding['role']=='shot_reference' else '人物身份、独立服装或场景外观参考'}" for index,binding in enumerate(bindings))
        payload.update(render_node('video_render',{'motion':shot['motion_prompt'],'references':references}))
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
        if task:validate_frame(db,task)
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
            if task:validate_frame(db,task)
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
