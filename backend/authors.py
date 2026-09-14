"""Shared author production workflow for the account-free demo."""
import hashlib
import asyncio
import json
import time
from threading import Lock
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from .db import Session, Record, Task, uid, record_dict, task_dict, generation_debug
from . import creative, director
from . import zhihu_stories
from .catalog import labels
from .skill_runtime import call_node
from .production_nodes import NODES, queue_node, node_snapshots, is_node_task, phase_for_saved_state

router = APIRouter(prefix='/api/author', tags=['author'])
open_lock = Lock()
attempt_lock = Lock()
def get_work(db, pid):
    row = db.get(Record, pid)
    if not row or row.kind != 'author_project':
        raise HTTPException(404, '作品不存在')
    return row

@router.post('/stories/{work_id}/open')
def open_story(work_id: str):
    if not zhihu_stories.valid_id(work_id):
        raise HTTPException(422, '故事标识格式无效。')
    pid = 'work_zhihu_' + hashlib.sha256(work_id.encode()).hexdigest()[:32]
    with open_lock:
        with Session() as db:
            existing = db.get(Record, pid)
        if existing is None:
            listing = zhihu_stories.stories()
            selected = next((x for x in listing['items'] if x['work_id'] == work_id), None)
            if not selected:
                raise HTTPException(404, '请从微小说目录选择作品。')
            if '脑洞' not in labels(selected.get('labels')):
                raise HTTPException(422, '当前制作流程仅支持脑洞类微小说。')
            imported = zhihu_stories.import_story(work_id)
            source = imported['story']
            with Session.begin() as db:
                db.add(Record(id=pid, kind='author_project', data={
                    'title': selected.get('title') or source['title'], 'source_id': source['id'],
                    'zhihu_work_id': work_id, 'stage': 'style', 'workflow': 'author-brainstorm-v4',
                    'source_warning': imported.get('warning') if imported.get('stale') else None,
                }))
    return workspace(pid)

@router.get('/projects')
def projects():
    with Session() as db:
        return [record_dict(r) for r in db.scalars(select(Record).where(Record.kind=='author_project').order_by(Record.created.desc()))]

@router.get('/projects/{pid}')
def workspace(pid: str):
    with Session() as db:
        row = get_work(db, pid); data = record_dict(row); data['workspace_at']=data['progress_at']=time.time()
        source = db.get(Record, row.data.get('source_id', ''))
        data['source'] = {k: source.data.get(k) for k in ('title','work_id','author_name','labels','content','source','source_url','fetched_at','completeness')} if source else None
        heartbeat = db.get(Record, 'worker_heartbeat')
        data['worker_online'] = bool(heartbeat and time.time() - heartbeat.data.get('at', 0) < 120)
        task = db.get(Task, row.data.get('supervisor', ''))
        data['task'] = task_dict(task) if task else None
        recommendation=db.get(Task,row.data.get('recommend_task',''))
        data['recommend_task_status']=task_dict(recommendation) if recommendation else None
        sid = row.data.get('director_id')
        all_tasks=list(db.scalars(select(Task).order_by(Task.created)))
        tasks = [t for t in all_tasks if (sid and any(t.payload.get(k)==sid for k in ('creative_id','director_id','preproduction_id','project_id'))) or is_node_task(t,row)]
        data['jobs'] = [task_dict(t) for t in tasks]
        data['production_steps']=node_snapshots(db,row)
        data['retryable_images'] = retryable_images(db,row)
        director_version=db.get(Record,sid).version if sid and db.get(Record,sid) else None
        data['outputs'] = completed_outputs(tasks,sid,director_version)
    data['creative'] = creative.workspace(sid) if sid else None
    data['display_stage']=data['stage']
    if data['stage'] in ('producing','compositing') and data['creative']:
        try:data['display_stage']=phase_for_saved_state(data['creative']['stage'])
        except HTTPException:pass
    data['asset_redesign_required']=bool(data['creative'] and data['creative'].get('items') and data['creative'].get('asset_schema')!=creative.asset_sheets.VERSION)
    if data.get('stage') in ('film_review','published'):
        from .skill_runtime import public,snapshot
        data['review_skill']=public(snapshot('film_review'))
    if data.get('stage')=='assets_review':
        from .skill_runtime import public,snapshot
        data['asset_review_skill']=public(snapshot('asset_review'))
    return data


def completed_outputs(tasks,director_id=None,director_version=None):
    replaced={t.payload.get('revision_of') for t in tasks if t.payload.get('revision_of')}
    replaced.update(tid for t in tasks if t.kind=='art_design' and t.status=='completed' for tid in t.result.get('replaced_assets',[]))
    outputs=[]
    for task in tasks:
        media=task.result.get('media')
        if director_id and task.payload.get('director_id')==director_id and task.payload.get('director_version')!=director_version:
            continue
        if task.id not in replaced and task.status=='completed' and task.kind in ('image','video') and isinstance(media,str) and media.startswith('/media/'):
            debug=generation_debug(task)
            outputs.append({'id':task.id,'kind':task.kind,'title':task.payload.get('title') or task.payload.get('shot_id') or '已完成素材','media':media,**({'generation':debug} if debug else {}),**({'production_phase':task.payload['production_phase']} if task.payload.get('production_phase') else {})})
    return outputs


def project_progress(pid):
    with Session() as db:
        row=get_work(db,pid);sid=row.data.get('director_id');observed_at=time.time()
        current=db.get(Record,sid) if sid else None
        tasks=[t for t in db.scalars(select(Task).order_by(Task.created)) if (sid and any(t.payload.get(k)==sid for k in ('creative_id','director_id','preproduction_id','project_id'))) or is_node_task(t,row)]
        supervisor=db.get(Task,row.data.get('supervisor',''))
        recommendation=db.get(Task,row.data.get('recommend_task',''))
        return {'id':pid,'stage':row.data['stage'],'run_id':row.data.get('run_id'),'progress_at':observed_at,'jobs':[task_dict(t) for t in tasks],
                'task':task_dict(supervisor) if supervisor else None,
                'recommend_task_status':task_dict(recommendation) if recommendation else None,
                'outputs':completed_outputs(tasks,sid,current.version if current else None),'production_steps':node_snapshots(db,row)}


@router.get('/projects/{pid}/events')
async def project_events(pid:str,request:Request):
    initial=await run_in_threadpool(project_progress,pid)
    async def events():
        snapshot=initial;previous='';heartbeat=time.monotonic()
        while not await request.is_disconnected():
            encoded=json.dumps({key:value for key,value in snapshot.items() if key!='progress_at'},ensure_ascii=False,separators=(',',':'))
            if encoded!=previous:
                yield 'data: '+json.dumps(snapshot,ensure_ascii=False,separators=(',',':'))+'\n\n'
                previous=encoded;heartbeat=time.monotonic()
            elif time.monotonic()-heartbeat>=12:
                yield ': keep-alive\n\n';heartbeat=time.monotonic()
            tasks=[*snapshot['jobs'],snapshot.get('task'),snapshot.get('recommend_task_status')]
            active=any(task and task['status'] in creative.BUSY for task in tasks)
            await asyncio.sleep(.35 if active else 2)
            snapshot=await run_in_threadpool(project_progress,pid)
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'})

class Start(creative.Style):
    restart:bool=False
    restart_from:Literal['style','preparing']='style'


def retryable_images(db,work):
    from .image_errors import can_retry
    sid=work.data.get('director_id')
    run=db.get(Record,'creative_'+sid) if sid else None
    if work.data['stage'] not in ('preparing','assets_review') or not run or run.data.get('stage')!='assets_review':return []
    if run.data.get('asset_schema')!=creative.asset_sheets.VERSION:return []
    return [{'task_id':item['task_id'],'name':item['name']} for item in run.data.get('items',[])
        if (task:=db.get(Task,item['task_id'])) and can_retry(task)]


class ImageRetry(BaseModel):
    confirm_paid:bool=False


@router.post('/projects/{pid}/images/{tid}/retry')
def retry_image(pid:str,tid:str,body:ImageRetry):
    """Retry one rejected base image, keeping the approved design and other images."""
    creative.paid(body)
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        if tid not in {item['task_id'] for item in retryable_images(db,row)}:
            raise HTTPException(409,'该图片当前不能单独重试；请检查是否已被替换、属于旧轮次，或调用结果仍不确定。')
        sid=row.data['director_id'];creative.get_project(db,sid);creative.no_active(db,sid)
        run=creative.get_run(db,sid);old=db.get(Task,tid)
        saved=old.result.get('generation_request') or {}
        prompt=saved.get('prompt') or old.payload.get('prompt')
        if not prompt:raise HTTPException(409,'没有可恢复的图片提示词，请重新准备素材。')
        task=Task(id=uid('image'),kind='image',payload={**old.payload,'prompt':prompt,'revision_of':tid})
        db.add(task)
        creative.prep.invalidate_downstream_references(db,sid,'失败的基础素材已重试并建立新版本',task.id)
        run.data={**run.data,'items':[{**item,'task_id':task.id} if item['task_id']==tid else item for item in run.data['items']]}
        run.version+=1
        supervisor=db.get(Task,row.data.get('supervisor',''))
        if supervisor and supervisor.status in creative.BUSY:raise HTTPException(409,'制作流程仍在运行，请等待后再处理。')
        if supervisor:supervisor.status='superseded'
        old.status='superseded'  # Keep the original rejection and request for history.
        row.data={k:v for k,v in row.data.items() if k!='assets_confirmed_at'}
        schedule(db,row,'preparing')
        db.flush()
        return {'queued':True,'task':task_dict(task)}


@router.post('/projects/{pid}/redesign')
def redesign(pid:str,body:Start):
    with Session.begin() as db:
        row=get_work(db,pid)
        if row.data['stage']!='assets_review':raise HTTPException(409,'当前不在人物与场景确认阶段。')
        creative.redesign(db,row.data['director_id'],body)
        row.data={**row.data,'art':body.art,'tone':body.tone,'workflow':'author-brainstorm-v4'}
        schedule(db,row,'preparing')
    return {'queued':True}

@router.post('/projects/{pid}/recommend')
def recommend(pid: str, body: creative.Publish):
    if not body.confirm: raise HTTPException(422, '请确认调用语言模型推荐风格')
    with Session.begin() as db:
        row=get_work(db,pid)
        old=db.get(Task,row.data.get('recommend_task',''))
        if old and old.status in creative.BUSY: return task_dict(old)
        source=db.get(Record,row.data['source_id'])
        creative.paid(creative.Style(art='推荐画风',tone='依据原著',confirm_paid=True))
        task=Task(id=uid('styles'),kind='author_styles',message='已加入队列，等待模型开始构思',payload={'mode':'live','source':source.data['content'],'work_id':pid})
        db.add(task);row.data={**row.data,'recommend_task':task.id};db.flush();return task_dict(task)

class StyleOption(BaseModel):
    art: str = Field(min_length=2,max_length=500)
    tone: str = Field(min_length=2,max_length=300)
    reason: str = Field(min_length=2,max_length=500)
    prompt: str = Field(min_length=10,max_length=500,description='可直接复用的纯画风提示词，只写媒介、线稿、明暗、色板、材质，不含人物或剧情')

class Options(BaseModel):
    approach: str = Field(default='',max_length=1200,description='给作者看的简短构思摘要，先于风格方案输出')
    options: list[StyleOption] = Field(min_length=3,max_length=5)

def recommend_styles(task_id,payload,owner=None):
    from .style_progress import style_preview
    live={'phase':'preparing','started_at':time.time(),'summary':'','options':[],'events':[]}
    last_saved=0.0
    messages={
        'preparing':'已读取原文，正在准备风格推荐',
        'connecting':'正在连接模型，等待构思响应',
        'connected':'模型已响应，等待构思内容',
        'reasoning':'模型正在构思，等待可展示的摘要',
        'buffered':'模型一次性返回内容，正在检查方案',
        'validating':'内容已接收，正在检查风格方案',
        'ready':'风格方案已就绪，可以比较选择',
    }

    def save(event,content='',force=False):
        nonlocal last_saved
        phase='drafting' if event=='delta' else event
        preview=style_preview(content) if content else {}
        now=time.time()
        first_text=bool(preview.get('summary')) and not live['summary']
        option_count=len(preview.get('options',[]))
        new_option=option_count>len(live['options'])
        if not force and phase==live['phase'] and now-last_saved<.2 and not first_text and not new_option:
            return
        message=messages.get(phase) or (f'正在展开第 {option_count} 个风格方案' if option_count else '构思摘要正在生成')
        if not live['events'] or live['events'][-1]['phase']!=phase:
            live['events']=[*live['events'],{'phase':phase,'message':message,'at':now}][-10:]
        live.update(preview,phase=phase,updated_at=now)
        with Session.begin() as db:
            task=db.get(Task,task_id)
            if not task or task.status!='running' or (owner is not None and task.owner!=owner):
                raise creative.ProviderError('本次风格任务已停止，旧输出不再更新。')
            task.result={**task.result,'live':dict(live)}
            task.message=message
        last_saved=now

    save('preparing',force=True)
    options,_=call_node(creative.chat_json,'style_options',{'source':payload['source'],'schema':Options.model_json_schema()},task_id,'fast',
        validator=Options.model_validate,on_event=save)
    result=options.model_dump()
    with Session.begin() as db:
        task=db.get(Task,task_id)
        if not task or task.status!='running' or (owner is not None and task.owner!=owner):
            raise creative.ProviderError('本次风格任务已停止，旧输出不再更新。')
        row=db.get(Record,payload['work_id']);row.data={**row.data,'recommendations':result['options']}
    save('ready',json.dumps(result,ensure_ascii=False),force=True)


def recommendation_snapshot(pid,task_id):
    with Session() as db:
        get_work(db,pid)
        task=db.get(Task,task_id)
        if not task or task.kind!='author_styles' or task.payload.get('work_id')!=pid:
            raise HTTPException(404,'风格任务不存在')
        return task_dict(task)


@router.get('/projects/{pid}/recommendations/{task_id}/events')
async def recommendation_events(pid:str,task_id:str,request:Request):
    # Stream only this task's display data; no model requests or whole-story polling.
    initial=await run_in_threadpool(recommendation_snapshot,pid,task_id)
    async def events():
        snapshot=initial
        previous=''
        heartbeat=time.monotonic()
        while not await request.is_disconnected():
            encoded=json.dumps(snapshot,ensure_ascii=False,separators=(',',':'))
            if encoded!=previous:
                yield 'data: '+encoded+'\n\n'
                previous=encoded
                heartbeat=time.monotonic()
            elif time.monotonic()-heartbeat>=12:
                yield ': keep-alive\n\n'
                heartbeat=time.monotonic()
            if snapshot['status'] not in creative.BUSY:
                break
            await asyncio.sleep(.3)
            snapshot=await run_in_threadpool(recommendation_snapshot,pid,task_id)
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'})

@router.post('/projects/{pid}/resume')
def resume(pid: str):
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        if row.data['stage'] not in ('preparing','producing','compositing',*NODES):raise HTTPException(409,'当前无需恢复')
        old=db.get(Task,row.data.get('supervisor',''))
        if old and old.status in creative.BUSY:raise HTTPException(409,'当前制作节点仍在运行。')
        rejected=retryable_images(db,row)
        if rejected:raise HTTPException(409,'请先重试失败图片：'+'、'.join(item['name'] for item in rejected)+'。其他已完成素材会保留。')
        if row.data['stage']=='preparing' and row.data.get('director_id'):
            creative.resume_saved_design(db,row.data['director_id'])
        if row.data['stage']=='storyboarding' and row.data.get('director_id'):
            creative.resume_saved_storyboard(db,row.data['director_id'])
        if row.data['stage'] in NODES:queue_node(db,row,row.data['stage'])
        elif row.data['stage'] in ('producing','compositing'):
            run=creative.get_run(db,row.data['director_id'])
            if old:old.status='superseded'
            queue_node(db,row,phase_for_saved_state(run.data['stage']))
        else:schedule(db,row,row.data['stage'])
    return {'queued':True}

def schedule(db, row, phase):
    old = db.get(Task, row.data.get('supervisor', ''))
    if old and old.status in creative.BUSY: raise HTTPException(409, '后台任务正在运行')
    task = Task(id=uid('authorflow'), kind='author_flow', payload={'work_id': row.id, 'phase': phase,'run_id':row.data.get('run_id')})
    db.add(task); row.data = {**row.data, 'supervisor': task.id, 'stage': phase}; row.version += 1

@router.post('/projects/{pid}/start')
def start(pid: str, body: Start):
    creative.paid(body)
    with attempt_lock,Session.begin() as db:
        row = get_work(db, pid)
        if row.data['stage'] != 'style' and not body.restart: raise HTTPException(409, '制作已开始')
        supervisor=db.get(Task,row.data.get('supervisor',''))
        if supervisor and supervisor.status in creative.BUSY:raise HTTPException(409,'当前制作仍在运行，完成或停止后才能重新测试。')
        if row.data.get('director_id'):creative.no_active(db,row.data['director_id'])
        history=list(row.data.get('attempt_history',[]))
        run_id=uid('attempt')
        previous_id=row.data.get('director_id')
        if row.data.get('director_id') or row.data.get('supervisor'):
            history.append({k:row.data.get(k) for k in ('run_id','stage','art','tone','director_id','supervisor','release_id','production_nodes') }|{'saved_at':time.time(),'invalidated_from':body.restart_from,'superseded_by_run':run_id})
        if previous_id:
            previous=db.get(Record,previous_id)
            if previous:
                previous.data={**previous.data,'archived':True,'status':'superseded','superseded_by_run':run_id,'archive_note':'上游重新测试，后续结果失效并保留为历史记录'}
                previous.version+=1
            for release in db.scalars(select(Record).where(Record.kind=='reader_release')):
                if release.data.get('director_id')==previous_id or release.id==row.data.get('release_id'):
                    release.data={**release.data,'status':'superseded','superseded_by_run':run_id};release.version+=1
        data={k:v for k,v in row.data.items() if k not in ('director_id','supervisor','release_id','assets_confirmed_at','production_nodes')}
        row.data = {**data,'attempt_history':history,'run_id':run_id, 'art': body.art, 'tone': body.tone, 'workflow':'author-brainstorm-v6'}
        schedule(db, row, 'preparing')
    return {'queued': True}

class Confirm(BaseModel):
    confirm: bool = False
    confirm_paid: bool = False

@router.post('/projects/{pid}/generate')
def generate(pid: str, body: Confirm):
    creative.paid(body)
    if not body.confirm: raise HTTPException(422, '请确认人物和场景图片满意')
    with attempt_lock,Session.begin() as db:
        row = get_work(db, pid)
        if row.data['stage'] != 'assets_review': raise HTTPException(409, '当前不能开始制作')
        creative.no_active(db,row.data['director_id'])
        run = creative.get_run(db, row.data['director_id'])
        for item in run.data['items']: creative.image_asset(db, item['task_id'])
        row.data = {**row.data, 'assets_confirmed_at': time.time()}
        from .skill_runtime import public,snapshot
        db.add(Record(id=uid('audit'),kind='audit',data={'target':row.id,'action':'author_asset_review','node_skill':public(snapshot('asset_review'))}))
        queue_node(db, row, 'storyboarding')
    return {'queued': True}

@router.post('/projects/{pid}/feedback')
def feedback(pid: str, body: creative.Feedback):
    with Session() as db:
        row = get_work(db, pid); sid = row.data.get('director_id')
        if row.data['stage'] not in ('assets_review','film_review'): raise HTTPException(409, '请等待本轮完成')
    result = creative.feedback(sid, body)
    if row.data['stage']=='film_review':
        with Session.begin() as db:
            row=get_work(db,pid)
            mapping=dict(row.data.get('production_nodes',{}));prior=db.get(Task,mapping.pop('rendering',''))
            if prior:prior.status='superseded'
            row.data={**row.data,'production_nodes':mapping}
            queue_node(db,row,'rendering')
    return result

@router.post('/projects/{pid}/publish')
def publish(pid: str, body: creative.Publish):
    with Session() as db:
        row = get_work(db, pid)
        if row.data['stage'] not in ('film_review','published'): raise HTTPException(409, '成片尚未完成')
        sid = row.data['director_id']
    result = creative.publish(sid, body)
    with Session.begin() as db:
        row=get_work(db,pid);row.data={**row.data,'stage':'published','release_id':result['id']}
    return result

def flow(task_id, payload):
    """One persisted scheduling transition per worker invocation; no browser polling dependency."""
    with Session() as db:
        row=db.get(Record,payload['work_id']); data=dict(row.data)
        if payload.get('run_id')!=data.get('run_id'):raise HTTPException(409,'此任务属于已失效的旧轮次，不会更新当前制作。')
        sid=data.get('director_id')
    from .workflows import WORKFLOWS
    if data.get('workflow','author-brainstorm-v1') not in WORKFLOWS:
        raise HTTPException(409,'制作工作流版本不可用，请由后台处理')
    if not sid:
        # Recover a director created before a supervisor interruption, rather than enqueue a duplicate.
        request_key=row.id+':'+data['run_id'] if data.get('run_id') else None
        existing=director.projects(data['source_id']) if not request_key else []
        result={'project_id':existing[0]['id']} if existing else director.create(director.Create(source_id=data['source_id'],brief='作者画风：'+data['art']+'；剧情气质：'+data['tone']+'。保留原著因果，完成一个起承转合明确的短场景。',confirm_paid=True,request_key=request_key))
        with Session.begin() as db:
            row=db.get(Record,payload['work_id']);row.data={**row.data,'director_id':result['project_id']}
        return False
    with Session() as db:
        project=db.get(Record,sid); origin=db.get(Task,project.data['task_id']); run=db.get(Record,'creative_'+sid)
        tasks=[t for t in db.scalars(select(Task)) if any(t.payload.get(k)==sid for k in ('creative_id','director_id','preproduction_id','project_id'))]
        if any(t.status in creative.BUSY for t in tasks): return False
    if not run:
        if origin.status!='completed': raise HTTPException(409,'原文分析未完成，请在后台处理任务后继续')
        creative.design(sid,creative.Style(art=data['art'],tone=data['tone'],confirm_paid=True));return False
    status=run.data['stage']
    if status=='designing':
        with Session() as db:design_task=db.get(Task,run.data.get('watch',''))
        reason=design_task.message if design_task else '设计任务记录缺失'
        raise HTTPException(409,'基础素材设计已暂停：'+reason)
    if status=='assets_review' and payload['phase']=='preparing':
        with Session() as db:
            for item in run.data['items']:creative.image_asset(db,item['task_id'])
        with Session.begin() as db:
            row=db.get(Record,payload['work_id']);row.data={**row.data,'stage':'assets_review'}
        return True
    if payload['phase']!='producing': raise HTTPException(409,'流程阶段不匹配')
    with Session.begin() as db:
        legacy=db.get(Task,task_id);work=get_work(db,payload['work_id'])
        if legacy and legacy.kind=='author_flow' and legacy.status=='running' and work.data.get('supervisor')==task_id:
            phase=phase_for_saved_state(status)
            legacy.status='completed';legacy.progress=100;legacy.message='已将保存的制作进度移交独立节点'
            replacement=queue_node(db,work,phase)
            legacy.result={**legacy.result,'handed_off_to':replacement.id}
            return True
    if status in ('assets_review','fittings_review','trials_review','samples_review','frames_review'):
        creative.advance(sid,creative.Continue(stage=status,confirm_review=True,confirm_paid=True),automatic=status!='assets_review')
        return False
    if status=='videos_review':
        result=creative.production.workspace(sid)
        if not result['shots'] or any(not s['video_task'] or s['video_task']['status']!='completed' or not s['clip'] for s in result['shots']):
            raise HTTPException(409,'部分镜头未完成，已保留结果，请在后台处理失败任务')
        with Session.begin() as db:
            row=db.get(Record,payload['work_id']);row.data={**row.data,'stage':'film_review'}
        return True
    raise HTTPException(409,'制作阶段未完成，请查看后台任务诊断')
