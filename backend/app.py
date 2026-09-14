import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI,HTTPException,UploadFile,File,Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from sqlalchemy import func,select,update
from .db import Session,Record,Task,DATA,init_db,uid,record_dict,task_dict
from .seed import seed
from .domain import state_at,validate_plan,Plan,ALLOWED,LABELS,VALUE_LABELS
from .providers import settings
from .local_config import save_config
from .zhihu_stories import router as story_router
from .director import router as director_router
from .billing import router as billing_router
from .production import router as production_router,reader as reader_router
from .catalog import router as catalog_router
from .video_storage import router as storage_router, generation_snapshot, register_artifact, attach_artifact
from .video_files import StorageError

@asynccontextmanager
async def lifespan(app):
    init_db();seed();yield

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,title='叙间 · 本地工作台',lifespan=lifespan)
app.include_router(story_router)
app.include_router(director_router)
app.include_router(billing_router)
app.include_router(production_router)
app.include_router(reader_router)
app.include_router(catalog_router)
app.include_router(storage_router)
from .consistency import router as consistency_router
app.include_router(consistency_router)
from .preproduction import router as preproduction_router
app.include_router(preproduction_router)
from .creative import router as creative_router
app.include_router(creative_router)
from .authors import router as author_router
app.include_router(author_router)
app.mount('/media',StaticFiles(directory=DATA/'media'),name='media')

@app.exception_handler(StorageError)
async def storage_error(request:Request,exc:StorageError):
    return JSONResponse({'detail':str(exc)},status_code=422)

@app.middleware('http')
async def local_only(request:Request,call_next):
    # Shared demo workspace. Keep the local browser origin guard for model operations.
    if request.method not in ['GET','HEAD','OPTIONS']:
        origin=request.headers.get('origin')
        if origin and origin not in ['http://127.0.0.1:3000','http://localhost:3000','http://127.0.0.1:8000','http://localhost:8000']:
            return JSONResponse({'detail':'仅允许本地工作台发起操作'},status_code=403)
    path=request.url.path
    response=await call_next(request)
    if path.startswith('/api/') or path.startswith('/media/'):
        # Preserve immediate SSE delivery through the web proxy: gzip otherwise buffers
        # an unfinished event stream even though the backend has already yielded data.
        streaming=response.headers.get('content-type','').startswith('text/event-stream')
        response.headers['Cache-Control']='no-store, no-transform' if streaming else 'no-store'
    return response

def require(db,id,kind=None):
    row=db.get(Record,id)
    if not row or (kind and row.kind!=kind):raise HTTPException(404,'记录不存在')
    return row

@app.get('/api/health')
def health():
    with Session() as db:
        worker=db.get(Record,'worker_heartbeat')
        return {'ok':True,'worker_online':bool(worker and time.time()-worker.data['at']<120),'database':'PostgreSQL' if db.bind.dialect.name=='postgresql' else 'SQLite 本地模式'}


@app.get('/api/node-skills')
def node_skills():
    from .skill_runtime import catalog
    return {'version':'1.0.0','nodes':catalog()}


@app.get('/api/node-skills/{node}')
def node_skill_detail(node:str):
    from .skill_runtime import public,snapshot
    from .providers import ProviderError
    try:binding=snapshot(node)
    except ProviderError as exc:raise HTTPException(404,str(exc)) from None
    return {**public(binding),'effective_instructions':binding['system']}

@app.get('/api/platform')
def platform():
    from .workflows import WORKFLOWS
    with Session() as db:
        works=[record_dict(r) for r in db.scalars(select(Record).where(Record.kind=='author_project').order_by(Record.created.desc()))]
        published_count=db.scalar(select(func.count()).select_from(Record).where(Record.kind=='reader_release'))
        project_works={w['director_id']:w['id'] for w in works if w.get('director_id')}
        recommendation_works={w['recommend_task']:w['id'] for w in works if w.get('recommend_task')}
        tasks=[]
        for task in db.scalars(select(Task).order_by(Task.created.desc())):
            work_id=task.payload.get('work_id') or recommendation_works.get(task.id)
            if not work_id:
                work_id=next((project_works[task.payload[k]] for k in ('creative_id','director_id','preproduction_id','project_id') if task.payload.get(k) in project_works),None)
            tasks.append({**task_dict(task),'work_id':work_id})
    return {'workflows':WORKFLOWS,'works':works,'tasks':tasks,'published_count':published_count,'health':health()}

@app.get('/api/bootstrap')
def bootstrap():
    with Session() as db:
        records=list(db.scalars(select(Record).where(Record.kind.in_(['story','asset','clip','audit']))))
        return {'story':next(record_dict(r) for r in records if r.kind=='story'),
                'assets':[record_dict(r) for r in records if r.kind=='asset'],
                'clips':[record_dict(r) for r in records if r.kind=='clip'],
                'audits':[record_dict(r) for r in records if r.kind=='audit'],
                'labels':LABELS,'values':VALUE_LABELS,'settings':settings()}

@app.get('/api/settings')
def get_settings():return settings()

class SettingsInput(BaseModel):
    values:dict[str,str]

@app.post('/api/settings')
def update_settings(body:SettingsInput):
    with Session() as db:
        if db.scalar(select(Task.id).where(Task.status.in_(['queued','running','waiting'])).limit(1)):
            raise HTTPException(409,'请先等待或停止当前任务，再修改模型配置。')
    try: save_config(body.values)
    except ValueError as exc: raise HTTPException(422,str(exc)) from None
    except OSError: raise HTTPException(500,'本地配置保存失败，请检查文件权限。') from None
    return settings()

@app.post('/api/sessions')
def create_session():
    with Session.begin() as db:
        story=require(db,'story_demo')
        entries=[]
        for cid in story.data['original_ids']:
            clip=require(db,cid,'clip')
            if clip.data['status']!='approved':raise HTTPException(409,'原版包含未通过审核的片段')
            entries.append({'clip_id':cid,'start':0,'end':clip.data['duration']})
        row=Record(id=uid('session'),kind='session',version=1,data={'story_id':'story_demo','story_version':story.version,'entries':entries,'active_task':None})
        db.add(row);db.flush();return record_dict(row)

@app.get('/api/sessions/{sid}')
def get_session(sid:str):
    with Session() as db:return record_dict(require(db,sid,'session'))

@app.get('/api/sessions/{sid}/state')
def get_state(sid:str,index:int=0,offset:float=0):
    with Session() as db:
        session=require(db,sid,'session')
        clips={r.id:r.data for r in db.scalars(select(Record).where(Record.kind=='clip'))}
        try:state,history=state_at(session.data['entries'],clips,index,offset)
        except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc))
        return {'state':state,'history':history,'version':session.version}

class Rewrite(BaseModel):
    version:int
    index:int=Field(ge=0)
    offset:float=Field(ge=0,allow_inf_nan=False)
    text:str=Field(min_length=2,max_length=2000)
    mode:Literal['demo','live']='demo'
    auto_render:bool=False

@app.post('/api/sessions/{sid}/rewrite')
def rewrite(sid:str,body:Rewrite):
    if body.mode=='live' and not settings()['llm_configured']:raise HTTPException(422,'请先配置真实语言模型 API')
    with Session.begin() as db:
        session=require(db,sid,'session')
        if session.version!=body.version:raise HTTPException(409,'当前分支已更新，请刷新后重新提交')
        clips={r.id:r.data for r in db.scalars(select(Record).where(Record.kind=='clip'))}
        try:state,history=state_at(session.data['entries'],clips,body.index,body.offset)
        except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc))
        story=require(db,'story_demo').data
        task_id=uid('plan');version=session.version+1
        data={**session.data,'active_task':task_id}
        changed=db.execute(update(Record).where(Record.id==sid,Record.version==body.version).values(data=data,version=version))
        if not changed.rowcount:raise HTTPException(409,'另一条改写刚刚提交，请刷新')
        db.execute(update(Task).where(Task.session_id==sid,Task.status.in_(['queued','running','waiting'])).values(status='superseded',message='已被新的改写替代'))
        task=Task(id=task_id,kind='plan',session_id=sid,revision=version,payload={**body.model_dump(),'state':state,'history':history,
                  'entries':session.data['entries'],'story':story,'intent':'从暂停处改变后续；不改写已发生历史'})
        db.add(task)
        db.add(Record(id=uid('rewrite'),kind='rewrite',data={'session_id':sid,'revision':version,'text':body.text,'index':body.index,'offset':body.offset,'state':state,'task_id':task_id}))
        db.flush();return {'task':task_dict(task),'session':{'id':sid,'version':version,**data}}

class RenderRequest(BaseModel):
    plan_task_id:str
    version:int
    mode:Literal['demo','live']='demo'
    confirm_paid:bool=False

@app.post('/api/sessions/{sid}/render')
def render(sid:str,body:RenderRequest):
    with Session.begin() as db:
        session=require(db,sid,'session');plan_task=db.get(Task,body.plan_task_id)
        if session.version!=body.version:raise HTTPException(409,'分支已经更新')
        if not plan_task or plan_task.kind!='plan' or plan_task.session_id!=sid or plan_task.revision!=body.version or plan_task.status!='completed':
            raise HTTPException(409,'该方案不属于当前有效分支')
        plan=Plan.model_validate(plan_task.result['plan'])
        if plan.status!='ready' or validate_plan(plan,plan_task.payload['state']):raise HTTPException(422,'方案尚未通过检查')
        if body.mode=='live':
            cfg=settings()
            if not body.confirm_paid or not cfg['video_configured'] or not cfg['paid_enabled']:
                raise HTTPException(422,'请配置视频 API 并确认真实分镜生成')
        active=db.get(Task,session.data.get('active_task',''))
        if active and active.kind in ['render','bridge'] and active.status in ['queued','running','waiting']:return task_dict(active)
        task=Task(id=uid('render'),kind='render' if body.mode=='demo' else 'bridge',session_id=sid,revision=body.version,payload={**plan_task.payload,'plan':plan.model_dump(),'mode':body.mode})
        # An explicit demo render only; live video requires review and annotations before use.
        session.data={**session.data,'active_task':task.id}
        db.add(task);db.flush();return task_dict(task)

class CommitBridge(BaseModel):
    version:int
    task_id:str
    confirm_visual:bool=False

@app.post('/api/sessions/{sid}/commit-bridge')
def commit_bridge(sid:str,body:CommitBridge):
    from .domain import prefix_at,JOIN_STATE
    with Session.begin() as db:
        session=require(db,sid,'session');task=db.get(Task,body.task_id)
        if not body.confirm_visual:raise HTTPException(422,'请先确认首尾画面的服装、道具和位置可以衔接')
        if not task or task.kind!='bridge' or task.session_id!=sid or task.revision!=body.version or session.version!=body.version:
            raise HTTPException(409,'该过渡已经过期或不属于当前分支')
        if task.status!='needs_review' or not task.result.get('clip_ids'):raise HTTPException(409,'过渡视频尚未全部生成')
        state=dict(task.payload['state']);generated=[]
        for cid in task.result['clip_ids']:
            clip=require(db,cid,'clip').data
            if clip['status']!='approved' or not clip.get('annotated'):raise HTTPException(422,'请先完成每个过渡片段的时间标注与审核')
            row=require(db,cid,'clip');row.data={**row.data,'locked':True}
            for ev in clip['events']:state.update(ev['changes'])
            generated.append({'clip_id':cid,'start':0,'end':clip['duration']})
        if state!=JOIN_STATE:raise HTTPException(422,'审核后的事件仍不满足尾声接入条件')
        p=task.payload;entries=prefix_at(p['entries'],p['index'],p['offset'])+generated+[{'clip_id':'clip_end','start':0,'end':16}]
        changed=db.execute(update(Record).where(Record.id==sid,Record.version==body.version)
                           .values(data={**session.data,'entries':entries,'active_task':None},version=body.version+1))
        if not changed.rowcount:raise HTTPException(409,'分支已更新')
        task.status='completed';task.message='人工确认视觉衔接后，真实过渡已接入';task.progress=100
        db.add(Record(id=uid('audit'),kind='audit',data={'target':task.id,'action':'bridge_commit','note':'本地操作员确认时间标注与首尾视觉衔接'}))
        return {'ok':True}

@app.get('/api/tasks')
def tasks():
    with Session() as db:return [task_dict(r) for r in db.scalars(select(Task).order_by(Task.created.desc()).limit(100))]

@app.get('/api/sessions/{sid}/history')
def history(sid:str):
    with Session() as db:
        return [record_dict(r) for r in db.scalars(select(Record).where(Record.kind=='rewrite').order_by(Record.created.desc())) if r.data['session_id']==sid]

@app.post('/api/tasks/{tid}/cancel')
def cancel(tid:str):
    with Session.begin() as db:
        task=db.get(Task,tid)
        if not task:raise HTTPException(404,'任务不存在')
        if task.status in ['queued','running','waiting']:
            task.status='cancelled';task.message='本地流程已停止；供应商可能继续运行与计费'
            if task.kind=='bridge':
                for child_id in task.result.get('children',[]):
                    child=db.get(Task,child_id)
                    if child and child.status in ['queued','running','waiting']:
                        child.status='cancelled';child.message='父过渡已停止，未提交任务不再运行'
        return task_dict(task)

class ResumeTask(BaseModel):
    provider_id:str|None=Field(default=None,pattern=r'^[A-Za-z0-9_-]{3,200}$')

@app.post('/api/tasks/{tid}/resume')
def resume_task(tid:str,body:ResumeTask):
    with Session.begin() as db:
        task=db.get(Task,tid)
        if not task:raise HTTPException(404,'任务不存在')
        if task.status not in ['failed','needs_review']:raise HTTPException(409,'此任务当前不能恢复')
        if task.kind=='author_flow':
            work=db.get(Record,task.payload.get('work_id',''))
            if not work or task.payload.get('run_id')!=work.data.get('run_id') or task.id!=work.data.get('supervisor'):
                raise HTTPException(409,'此调度任务已被新轮次替代，不能恢复。')
        for key in ('director_id','project_id','preproduction_id','creative_id'):
            project=db.get(Record,task.payload.get(key,''))
            if project and project.kind=='director' and project.data.get('superseded_by_run'):
                raise HTTPException(409,'上游已重新测试，此任务的后续结果已经失效，不能恢复。')
        if task.session_id:
            session=require(db,task.session_id,'session')
            if session.version!=task.revision:raise HTTPException(409,'原任务属于旧分支，请在当前版本重新操作')
        if task.kind=='video':
            provider_id=task.result.get('provider_id') or body.provider_id
            if not provider_id:raise HTTPException(422,'请先在供应商控制台核实任务编号，不能自动重新收费提交')
            task.result={**task.result,'provider_id':provider_id,'submitted':time.time()}
        elif task.payload.get('mode')=='live':raise HTTPException(422,'该真实流程需要重新审查，请重新提交改写或处理子视频任务')
        task.status='queued';task.lease=0;task.owner='';task.message='已恢复，继续已有任务';return task_dict(task)

class AssetBody(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    type:Literal['character','scene','prop']='character'
    description:str=Field(min_length=1,max_length=2000)
    media:str=''

@app.post('/api/assets')
def create_asset(body:AssetBody):
    if body.media and not body.media.startswith('/media/'):raise HTTPException(422,'请使用已上传素材')
    with Session.begin() as db:
        row=Record(id=uid('asset'),kind='asset',data={**body.model_dump(),'status':'pending','palette':'cream','mutable':['description']})
        db.add(row);db.flush();return record_dict(row)

@app.post('/api/assets/{aid}/fork')
def fork_asset(aid:str,body:AssetBody):
    with Session.begin() as db:
        old=require(db,aid,'asset')
        row=Record(id=uid('asset'),kind='asset',data={**body.model_dump(),'status':'pending','palette':old.data.get('palette','cream'),'parent_id':aid,'mutable':['description']})
        db.add(row);db.flush();return record_dict(row)

class Review(BaseModel):
    status:Literal['approved','rejected']
    note:str=Field(default='',max_length=1000)

@app.post('/api/review/{rid}')
def review(rid:str,body:Review):
    with Session.begin() as db:
        row=require(db,rid)
        if row.kind not in ['asset','clip']:raise HTTPException(422,'只支持审核素材或片段')
        if row.data.get('locked'):raise HTTPException(409,'该片段已被播放版本引用，审核状态只读')
        if rid in ['clip_open','clip_turn','clip_end'] and body.status!='approved':
            raise HTTPException(409,'内置已发布原版为只读示例；请上传新片段制作另一版本')
        row.data={**row.data,'status':body.status,'review_note':body.note};row.version+=1
        db.add(Record(id=uid('audit'),kind='audit',data={'target':rid,'version':row.version,'action':body.status,'note':body.note,'reviewer':'本地操作员'}))
        return record_dict(row)

class EventInput(BaseModel):
    at:float=Field(ge=0,allow_inf_nan=False)
    text:str=Field(min_length=1,max_length=1200)
    changes:dict[str,str]

class Annotation(BaseModel):
    events:list[EventInput]=Field(max_length=30)

@app.post('/api/clips/{cid}/annotate')
def annotate(cid:str,body:Annotation):
    with Session.begin() as db:
        row=require(db,cid,'clip')
        if cid in ['clip_open','clip_turn','clip_end']:raise HTTPException(409,'已发布原版标注为只读')
        if row.data.get('locked'):raise HTTPException(409,'已被播放版本引用，请重新生成独立片段')
        for ev in body.events:
            if ev.at>row.data['duration']:raise HTTPException(422,'事件时间超过视频时长')
            if any(k not in ALLOWED or v not in ALLOWED[k] for k,v in ev.changes.items()):raise HTTPException(422,'存在未定义的状态取值')
        events=sorted([e.model_dump() for e in body.events],key=lambda e:e['at'])
        expected=row.data.get('expected_changes')
        if expected is not None:
            combined={}
            for ev in events:combined.update(ev['changes'])
            if combined!=expected:raise HTTPException(422,'事件状态必须完整对应本分镜通过检查的变化；若成片不符，请退回重新制作')
        row.data={**row.data,'events':events,'annotated':True,'status':'pending'};row.version+=1
        return record_dict(row)

@app.post('/api/upload')
async def upload(file:UploadFile=File(...)):
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in ['.png','.jpg','.jpeg','.webp','.mp4']:raise HTTPException(422,'支持 PNG、JPG、WEBP、MP4')
    path=DATA/'media'/f'{uid("upload")}{suffix}';total=0
    try:
        with path.open('wb') as f:
            while chunk:=await file.read(1024*1024):
                total+=len(chunk)
                if total>100*1024*1024:raise HTTPException(413,'第一版限制单个文件 100MB')
                f.write(chunk)
        if suffix!='.mp4':
            from PIL import Image
            with Image.open(path) as im:im.verify()
        else:
            clip_id=uid('clip')
            with Session.begin() as db:
                artifact=register_artifact(db,clip_id,path,{'origin':'upload','source_filename':file.filename})
                clip=Record(id=clip_id,kind='clip',data={'title':file.filename,
                    'demo':False,'status':'pending','narration':'上传片段，待人工标注','events':[],'entry_state':{},'assets':[],'annotated':False})
                attach_artifact(clip,artifact);db.add(clip)
                response={'media':clip.data['media'],'clip_id':clip_id,'artifact_id':artifact.id,'storage_schema_version':1}
            path.unlink(missing_ok=True)
            return response
        return {'media':f'/media/{path.name}'}
    except HTTPException:
        path.unlink(missing_ok=True);raise
    except Exception:
        path.unlink(missing_ok=True);raise HTTPException(422,'文件不是有效的图片或视频') from None

class VideoRequest(BaseModel):
    prompt:str=Field(min_length=5,max_length=4000)
    title:str=Field(default='真实视频测试',max_length=80)
    image_url:str|None=None
    asset_id:str|None=None
    confirm_paid:bool=False

@app.post('/api/video')
def video(body:VideoRequest):
    cfg=settings()
    if not body.confirm_paid or not cfg['paid_enabled'] or not cfg['video_configured']:
        raise HTTPException(422,'请配置视频 API、开启付费调用，并确认本次收费调用')
    with Session.begin() as db:
        payload={**body.model_dump(),'mode':'live'}
        if body.asset_id:
            if body.image_url: raise HTTPException(422,'本地参考图和外部参考图只能选择一个')
            if cfg['editable']['VIDEO_PROVIDER']!='minimax': raise HTTPException(422,'本地图片参考目前支持 MiniMax H3 视频接口')
            asset=require(db,body.asset_id,'asset')
            from .asset_workflow import validate_asset_origin
            validate_asset_origin(db,asset)
            if asset.data.get('director_id'):raise HTTPException(409,'导演镜头请在制作工作台生成视频，以校验一致性与小样审核。')
            if asset.data.get('status')!='approved': raise HTTPException(422,'请先审核参考图片')
            from .image_provider import local_frame_data
            from .providers import ProviderError
            try: local_frame_data(asset.data.get('media'))
            except (ProviderError,OSError,ValueError): raise HTTPException(422,'参考图片文件无效或尺寸不受支持') from None
            payload.update(asset_version=asset.version,generation_seconds=asset.data.get('generation_seconds'),
                           director_id=asset.data.get('director_id'),shot_id=asset.data.get('shot_id'),edit_seconds=asset.data.get('edit_seconds'))
        payload['input_mode']='reference_images' if body.asset_id or body.image_url else 'text'
        payload['input_snapshot']=generation_snapshot(db,payload,frame_asset=asset if body.asset_id else None,reference_mode=True)
        if body.asset_id:payload['reference_media']=[binding['file']['media'] for binding in payload['input_snapshot']['asset_bindings']]
        task=Task(id=uid('video'),kind='video',payload=payload)
        db.add(task);db.flush();return task_dict(task)

class ImageRequest(BaseModel):
    prompt:str=Field(min_length=5,max_length=4000)
    title:str=Field(default='生成的参考图片',min_length=1,max_length=80)
    confirm_paid:bool=False

@app.post('/api/image')
def image_generation(body:ImageRequest):
    cfg=settings()
    if not body.confirm_paid or not cfg['paid_enabled'] or not cfg['image_configured']:
        raise HTTPException(422,'请保存完整生图配置、开启付费调用，并确认本次收费调用')
    with Session.begin() as db:
        task=Task(id=uid('image'),kind='image',payload={**body.model_dump(),'mode':'live'})
        db.add(task);db.flush();return task_dict(task)

@app.post('/api/sessions/{sid}/export')
def export(sid:str):
    with Session.begin() as db:
        session=require(db,sid,'session')
        task=Task(id=uid('export'),kind='export',session_id=sid,revision=session.version,payload={'entries':session.data['entries'],'mode':'demo'})
        db.add(task);db.flush();return task_dict(task)
