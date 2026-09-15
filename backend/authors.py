"""Account-owned author production workflow."""
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
from .db import HISTORY_LIMIT, Session, Record, Task, uid, record_dict, task_dict, generation_debug
from . import creative, director
from . import zhihu_stories
from .catalog import labels
from .skill_runtime import call_node
from .production_nodes import (NODES, queue_node, node_snapshots, is_node_task, stopped_node,
                               stopped_stage_media)
# Imported under different names: the HTTP handlers below are also called retry_node / redo_node,
# and a handler that shadows the function it calls recurses into itself.
from .production_nodes import retry_node as retry_stage_node, redo_node as redo_stage_node

router = APIRouter(prefix='/api/author', tags=['author'])
open_lock = Lock()
attempt_lock = Lock()
def get_work(db, pid):
    """The project behind an HTTP request, refused when it belongs to somebody else."""
    row = work_row(db, pid)
    owner = row.data.get('owner')
    actor = current_actor()
    if owner and owner != actor:
        # Another account's work is not this visitor's work: it must not appear, and it must not be
        # resumable on someone else's bean wallet. Ownerless records are pre-account work.
        raise HTTPException(404, '作品不存在')
    if not owner:
        from .model_access import public_demo_mode

        # Pre-account work may only be claimed through ``open_story``. Hiding it here prevents a
        # visitor who guessed an old id from editing it before the ownership transition is atomic.
        if public_demo_mode():
            raise HTTPException(404, '作品不存在')
    return row


def work_row(db, pid):
    """The project row without an ownership test, for the worker that already holds a task.

    A queued task was authorized when it was created; its stored payer session can expire or be
    retired by the time the worker runs it, so the worker must not repeat the visitor check.
    """
    row = db.get(Record, pid)
    if not row or row.kind != 'author_project':
        raise HTTPException(404, '作品不存在')
    return row


def current_actor():
    """Who this request acts as, for work ownership. None in a local run without accounts."""
    from .model_access import current_actor_id

    return current_actor_id()


def work_id_for(work_id, actor):
    """Stable per-owner id for one story, so two accounts never share one project record."""
    return 'work_zhihu_' + hashlib.sha256(f'{work_id}|{actor}'.encode()).hexdigest()[:32]

@router.post('/stories/{work_id}/open')
def open_story(work_id: str):
    if not zhihu_stories.valid_id(work_id):
        raise HTTPException(422, '故事标识格式无效。')
    actor = current_actor()
    from .model_access import public_demo_mode
    if public_demo_mode() and not actor:
        raise HTTPException(401, '请先登录，再制作属于自己的漫剧版本。')
    pid = work_id_for(work_id, actor)
    with open_lock:
        with Session() as db:
            existing = db.get(Record, pid)
            if existing is not None and existing.kind == 'author_project':
                return workspace(pid)
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
                    'zhihu_work_id': work_id, 'stage': 'style', 'workflow': 'author-brainstorm-v7',
                    'owner': actor,
                    'source_warning': imported.get('warning') if imported.get('stale') else None,
                }))
    return workspace(pid)

@router.get('/projects')
def projects():
    with Session() as db:
        actor = current_actor()
        rows = list(db.scalars(select(Record).where(Record.kind == 'author_project').order_by(Record.created.desc())))
        from .model_access import public_demo_mode
        # A public visitor sees only their work. Ownerless records remain available in local mode;
        # online they are reached solely through the atomic one-time claim in ``open_story``.
        return [record_dict(r) for r in rows
                if r.data.get('owner') == actor or (not public_demo_mode() and not r.data.get('owner'))]

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
        current=db.get(Record,sid) if sid else None
        data['segments']=current.data.get('segments') if current else None
        # Per-episode progress drives the cut review panel and the publish/continue pause.
        data['episodes']=creative.episode_progress(sid) if sid else []
        repairs=current.data.get('board_repairs') if current else None
        data['board_repairs']=repairs if repairs else None
        # The text pre-review's findings are opinions for the author, not errors: showing them here
        # is why the panel calls them 复核提示 rather than a failed step.
        data['storyboard_review']=creative.review_notes(current)
    data['creative'] = creative.workspace(sid) if sid else None
    data['display_stage']=data['stage']
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
            if not active:
                break
            await asyncio.sleep(.35)
            snapshot=await run_in_threadpool(project_progress,pid)
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no'})

class Start(creative.Style):
    restart:bool=False
    restart_from:Literal['style','preparing']='style'


def retryable_images(db,work):
    from .image_errors import can_retry,needs_prompt_edit,task_error
    sid=work.data.get('director_id')
    run=db.get(Record,'creative_'+sid) if sid else None
    if work.data['stage'] not in ('preparing','assets_review') or not run or run.data.get('stage')!='assets_review':return []
    if run.data.get('asset_schema')!=creative.asset_sheets.VERSION:return []
    images=[]
    for item in run.data.get('items',[]):
        task=db.get(Task,item['task_id'])
        if not task or not can_retry(task):continue
        error=task_error(task) or {}
        saved=task.result.get('generation_request') or {}
        images.append({'task_id':task.id,'name':item['name'],'category':error.get('category',''),
            # A prompt the author may rewrite is sent back so the repair panel can offer the editor.
            'prompt':(saved.get('prompt') or task.payload.get('prompt') or '') if needs_prompt_edit(error) else '',
            'needs_prompt_edit':needs_prompt_edit(error)})
    return images


class ImageRetry(BaseModel):
    confirm_paid:bool=False
    # An optional replacement prompt: the provider repeats a content-policy rejection until the
    # author rewrites what the picture asks for.
    prompt:str|None=Field(default=None,min_length=10,max_length=6000)


def requeue_image(db,row,tid,prompt=None):
    """Queue one fresh version of a rejected base picture, keeping its design.

    The saved prompt is reused unless the author rewrote it; an edited prompt no longer follows the
    validated render contract, so the new task records that the text was changed by hand.
    """
    sid=row.data['director_id'];run=creative.get_run(db,sid);old=db.get(Task,tid)
    saved=old.result.get('generation_request') or {}
    edited=(prompt or '').strip()
    chosen=edited or saved.get('prompt') or old.payload.get('prompt')
    if not chosen:raise HTTPException(409,'没有可恢复的图片提示词，请重新准备素材。')
    payload={**old.payload,'prompt':chosen,'revision_of':tid}
    if edited:payload.update({'prompt_source':'manual_edit','prompt_edited':True})
    task=Task(id=uid('image'),kind='image',payload=payload)
    db.add(task)
    creative.prep.invalidate_downstream_references(db,sid,'失败的基础素材已重试并建立新版本',task.id)
    run.data={**run.data,'items':[{**item,'task_id':task.id} if item['task_id']==tid else item for item in run.data['items']]}
    run.version+=1
    old.status='superseded'  # Keep the original rejection and request for history.
    if edited:
        db.add(Record(id=uid('audit'),kind='audit',data={'target':task.id,'action':'image_prompt_edited',
            'replaced_task':tid,'note':'用户按供应商的内容限制手动修改了生图提示词','prompt':edited[:1500]}))
    return task


def open_repair_round(db,row):
    """Let the round finish once the queued repairs are back, and stop the old supervisor."""
    supervisor=db.get(Task,row.data.get('supervisor',''))
    if supervisor and supervisor.status in creative.BUSY:raise HTTPException(409,'制作流程仍在运行，请等待后再处理。')
    if supervisor:supervisor.status='superseded'
    row.data={k:v for k,v in row.data.items() if k!='assets_confirmed_at'}


@router.post('/projects/{pid}/images/{tid}/retry')
def retry_image(pid:str,tid:str,body:ImageRetry):
    """Retry one rejected base image, keeping the approved design and other images.

    A content-policy rejection (HTTP 451) is refused by every resubmission of the same text, so the
    author may send a rewritten prompt with this request; other failures keep their saved prompt.
    """
    creative.paid(body,'image')
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        entry=next((item for item in retryable_images(db,row) if item['task_id']==tid),None)
        if not entry:
            raise HTTPException(409,'该图片当前不能单独重试；请检查是否已被替换、属于旧轮次，或调用结果仍不确定。')
        if body.prompt is not None and not entry['needs_prompt_edit']:
            raise HTTPException(409,'这次失败不需要改写提示词：请直接重试这张图片；若要改画面，请在图片卡片填写修改意见。')
        sid=row.data['director_id'];creative.get_project(db,sid);creative.no_active(db,sid)
        task=requeue_image(db,row,tid,body.prompt)
        open_repair_round(db,row)
        # The supervisor can only finish once every rejected picture is back, so it is queued with
        # the last repair. Queueing it earlier left it waiting on the other pictures and refused the
        # visitor's next "重试这张图片" with "制作流程仍在运行".
        if not [item for item in retryable_images(db,row) if item['task_id']!=tid]:schedule(db,row,'preparing')
        db.flush()
        return {'queued':True,'task':task_dict(task)}


@router.post('/projects/{pid}/images/retry')
def retry_images(pid:str,body:ImageRetry):
    """Queue a fresh version of every rejected base picture in one action.

    The panel lists one button per picture, so a stopped run of several pictures used to be
    repaired one click at a time; a single visitor action must be able to bring the whole set back.
    Pictures whose prompt the provider already rejected are left to their own editor: resubmitting
    the same prompt would only be refused again.
    """
    creative.paid(body,'image')
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        retryable=retryable_images(db,row)
        images=[item for item in retryable if not item['needs_prompt_edit']]
        skipped=[item['task_id'] for item in retryable if item['needs_prompt_edit']]
        if not images:
            raise HTTPException(409,'当前没有可以直接重试的失败图片：内容违规的图片需要先修改提示词。')
        sid=row.data['director_id'];creative.get_project(db,sid);creative.no_active(db,sid)
        tasks=[requeue_image(db,row,item['task_id']) for item in images]
        open_repair_round(db,row)
        schedule(db,row,'preparing')
        db.flush()
        return {'queued':True,'tasks':[task_dict(task) for task in tasks],'needs_prompt_edit':skipped}


@router.post('/projects/{pid}/redesign')
def redesign(pid:str,body:Start):
    with Session.begin() as db:
        row=get_work(db,pid)
        if row.data['stage']!='assets_review':raise HTTPException(409,'当前不在人物与场景确认阶段。')
        creative.redesign(db,row.data['director_id'],body)
        row.data={**row.data,'art':body.art,'tone':body.tone,'workflow':'author-brainstorm-v7'}
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
        creative.paid(creative.Style(art='推荐画风',tone='依据原著',confirm_paid=True),'llm')
        task=Task(id=uid('styles'),kind='author_styles',message='已加入队列，等待模型开始构思',payload={'mode':'live','source':source.data['content'],'work_id':pid})
        db.add(task);row.data={**row.data,'recommend_task':task.id};db.flush();return task_dict(task)

class StyleOption(BaseModel):
    art: str = Field(min_length=2,max_length=500,description='简短的电影视觉方向名称')
    tone: str = Field(min_length=2,max_length=300)
    reason: str = Field(min_length=2,max_length=500)
    prompt: str = Field(min_length=10,max_length=500,description='可复用的全片电影视觉规则，包含成像媒介、构图、焦段、运动、灯光、调色与纹理，不编写具体镜头或新增剧情')

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
    """Continue a stopped run without touching what it already saved.

    Continuing used to call the node-retry path, so a button that promised to reuse saved results
    re-ran the node and rebuilt its media. It now only continues: anything that needs re-running
    says so and points at 重试 or 重做, which are the two actions that deliberately change work.
    """
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        if row.data['stage'] not in ('preparing',*NODES):raise HTTPException(409,'当前无需恢复')
        old=db.get(Task,row.data.get('supervisor',''))
        if old and old.status in creative.BUSY:raise HTTPException(409,'当前制作节点仍在运行。')
        rejected=retryable_images(db,row)
        if rejected:raise HTTPException(409,'请先重试失败图片：'+'、'.join(item['name'] for item in rejected)+'。其他已完成素材会保留。')
        if row.data['stage']=='preparing' and row.data.get('director_id'):
            creative.resume_saved_design(db,row.data['director_id'])
        if row.data['stage'] not in NODES:
            schedule(db,row,row.data['stage'])
        else:
            # Only stopped pictures and clips force that choice: they need a decision about whether
            # to re-run or throw away. A node whose saved result can carry on is continued below.
            if stopped_stage_media(db,row):
                raise HTTPException(409,'当前节点还有停下的画面或片段：请用「重试本节点」带上错误重跑，'
                                        '或用「重做本节点」删除本轮记录后重新开始。')
            # Continuing a storyboard node only re-queues the coordinator: it picks up the next
            # episode that still needs a board and reuses every episode already saved.
            queue_node(db,row,row.data['stage'])
    return {'queued':True}


class NodeRetry(BaseModel):
    confirm_paid:bool=False


@router.post('/projects/{pid}/retry-node')
def retry_node(pid:str,body:NodeRetry):
    with Session() as db:
        row=get_work(db,pid)
    # A retry re-runs the node that failed, so only that node's providers are required.
    phase=row.data.get('stage')
    creative.paid(body,*(('video',) if phase=='rendering' else ('llm',)))
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        task=retry_stage_node(db,row)
        db.flush()
        return {'queued':True,'task':task_dict(task)}


@router.post('/projects/{pid}/redo-node')
def redo(pid:str,body:NodeRetry):
    """Delete this node's work and everything after it, then run the node again from scratch."""
    with Session() as db:
        row=get_work(db,pid)
    phase=row.data.get('stage')
    creative.paid(body,*(('video',) if phase=='rendering' else ('llm',)))
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        task=redo_stage_node(db,row)
        db.flush()
        return {'queued':True,'task':task_dict(task)}


class StageRedo(BaseModel):
    stage: str
    confirm_paid: bool = False


REDO_STAGES = ('segments_review','preparing','assets_review','storyboarding','rendering','film_review','published')


def _drop_asset(db,asset_id):
    from .production_nodes import DATA as MEDIA_ROOT
    row=db.get(Record,asset_id)
    if not row:return
    media=(row.data or {}).get('media')
    if isinstance(media,str) and media.startswith('/media/'):
        path=(MEDIA_ROOT/'media'/media.removeprefix('/media/')).resolve()
        if path.is_relative_to((MEDIA_ROOT/'media').resolve()):path.unlink(missing_ok=True)
    db.delete(row)


def _drop_run(db,run):
    """Delete the design run with the pictures it produced; the author is starting this step over."""
    from .production_nodes import delete_task_outputs
    if not run:return []
    removed=[]
    for item in run.data.get('items') or []:
        task=db.get(Task,item.get('task_id',''))
        if not task:continue
        delete_task_outputs(db,task)
        asset_id=(task.result or {}).get('asset_id')
        if asset_id:_drop_asset(db,asset_id)
        removed.append(task.id)
        db.delete(task)
    db.delete(run)
    return removed


def _drop_downstream(db,sid,keep=()):
    """Delete every task of this run except the ones the new attempt still needs."""
    from .production_nodes import delete_task_outputs
    removed=[]
    for task in list(db.scalars(select(Task))):
        if task.id in keep:continue
        if not any(task.payload.get(key)==sid for key in ('creative_id','director_id','preproduction_id','project_id')):
            continue
        delete_task_outputs(db,task)
        removed.append(task.id)
        db.delete(task)
    return removed


def _reset_creative(db,sid,stage):
    run=db.get(Record,'creative_'+sid)
    if not run:return
    run.data={**{key:value for key,value in run.data.items()
                 if key in ('art','tone','run_id','production_split','direct_reference_inputs')},
              'stage':stage,'items':[]}
    run.version+=1


@router.post('/projects/{pid}/redo')
def redo_any(pid:str,body:StageRedo):
    """Redo one completed step: its work and everything after it is deleted, then it runs again.

    Redoing an early step therefore redoes the whole branch below it, and redoing the last finished
    step redoes only that step — the same button, and the scope is whatever depends on it.
    """
    target=body.stage
    if target not in REDO_STAGES:raise HTTPException(409,'这一步不能重做。')
    creative.paid(body,*(('video',) if target in ('rendering','film_review','published') else ('llm',)))
    with attempt_lock,Session.begin() as db:
        row=get_work(db,pid)
        order=list(REDO_STAGES)
        current=row.data.get('stage')
        if current not in REDO_STAGES:raise HTTPException(409,'当前制作还没有可重做的步骤。')
        if order.index(target)>order.index(current):raise HTTPException(409,'这一步还没有开始，不能重做。')
        sid=row.data.get('director_id')
        project=db.get(Record,sid) if sid else None
        if not project:raise HTTPException(409,'导演项目不存在，不能重做。')
        old=db.get(Task,row.data.get('supervisor',''))
        if old and old.status in creative.BUSY:raise HTTPException(409,'后台任务正在运行，请等待结束后重做。')
        removed=[]
        if target in ('storyboarding','rendering'):
            # These two stages are the production nodes; the node redo already deletes its own work
            # and the work of the nodes after it.
            row.data={**row.data,'stage':target}
            db.flush()
            task=redo_stage_node(db,row)
            db.flush()
            return {'queued':True,'stage':target,'task':task_dict(task),'removed':[]}
        if target=='segments_review':
            # The cut is remade from the same treatment; everything drawn or planned from the old
            # cut disappears with it, including the pictures that were made for those episodes.
            keep=set()
            origin=db.get(Task,project.data.get('task_id',''))
            if origin:keep.add(origin.id)
            removed=_drop_downstream(db,sid,keep)
            _drop_run(db,db.get(Record,'creative_'+sid))
            project.data={key:value for key,value in project.data.items()
                          if key not in ('segments','units','board','review','board_diagnostics',
                                         'board_chunk_diagnostics','board_progress','board_repairs',
                                         'preproduction_stamp','status','requires_preproduction')}
            project.data={**project.data,'status':'awaiting_preproduction'}
            project.version+=1
            row.data={**{key:value for key,value in row.data.items()
                         if key not in ('segments_approved_at','assets_confirmed_at','production_nodes')},
                      'stage':'segments_review'}
            if origin:
                origin.status='queued';origin.lease=0;origin.owner=''
                origin.message='正在按你的要求重新分析原文并切割情节'
                row.data={**row.data,'recut_task':origin.id,'supervisor':origin.id}
            db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'stage_redone',
                'stage':target,'discarded_task_ids':removed}))
            db.flush()
            return {'queued':True,'stage':'segments_review','recut':bool(origin),'removed':removed}
        if target in ('preparing','assets_review'):
            # Artwork is remade from the same treatment and the same cut.
            keep={db.get(Record,sid).data.get('task_id')}
            removed=_drop_downstream(db,sid,keep)
            _drop_run(db,db.get(Record,'creative_'+sid))
            project.data={**project.data,'status':'awaiting_preproduction','requires_preproduction':True}
            project.version+=1
            row.data={**{key:value for key,value in row.data.items()
                         if key not in ('segments_approved_at','assets_confirmed_at','production_nodes')},
                      'stage':'preparing'}
            creative.design(sid,creative.Style(art=row.data.get('art') or '沿用当前风格',
                tone=row.data.get('tone') or '沿用当前气质',confirm_paid=True))
            db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'stage_redone',
                'stage':target,'discarded_task_ids':removed}))
            db.flush()
            return {'queued':True,'stage':'preparing','removed':removed}
        # 审片与发布：删掉片段与已发布的版本，从漫剧生成重新做一次。
        removed=_drop_downstream(db,sid,keep={db.get(Record,sid).data.get('task_id')})
        _reset_creative(db,sid,'storyboard_ready')
        if target=='published':
            release=db.get(Record,f'release_{sid}')
            if release:db.delete(release)
        project.data={**project.data,'status':'approved'}
        project.version+=1
        row.data={**{key:value for key,value in row.data.items()
                     if key not in ('assets_confirmed_at','production_nodes','release_id')},
                  'stage':'rendering'}
        db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'stage_redone',
            'stage':target,'discarded_task_ids':removed}))
        db.flush()
        task=queue_node(db,row,'rendering',reopen=True)
        db.flush()
        return {'queued':True,'stage':'rendering','task':task_dict(task),'removed':removed}

def schedule(db, row, phase):
    old = db.get(Task, row.data.get('supervisor', ''))
    if old and old.status in creative.BUSY: raise HTTPException(409, '后台任务正在运行')
    task = Task(id=uid('authorflow'), kind='author_flow', payload={'work_id': row.id, 'phase': phase,'run_id':row.data.get('run_id')})
    db.add(task); row.data = {**row.data, 'supervisor': task.id, 'stage': phase}; row.version += 1

@router.post('/projects/{pid}/start')
def start(pid: str, body: Start):
    creative.paid(body,'llm','image')
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
            history=history[-HISTORY_LIMIT:]
        if previous_id:
            previous=db.get(Record,previous_id)
            if previous:
                previous.data={**previous.data,'archived':True,'status':'superseded','superseded_by_run':run_id,'archive_note':'上游重新测试，后续结果失效并保留为历史记录'}
                previous.version+=1
            for release in db.scalars(select(Record).where(Record.kind=='reader_release')):
                if release.data.get('director_id')==previous_id or release.id==row.data.get('release_id'):
                    release.data={**release.data,'status':'superseded','superseded_by_run':run_id};release.version+=1
        data={k:v for k,v in row.data.items() if k not in ('director_id','supervisor','release_id','assets_confirmed_at','production_nodes')}
        row.data = {**data,'attempt_history':history,'run_id':run_id, 'art': body.art, 'tone': body.tone, 'workflow':'author-brainstorm-v7'}
        schedule(db, row, 'preparing')
    return {'queued': True}

class Confirm(BaseModel):
    confirm: bool = False
    confirm_paid: bool = False


class SegmentReview(BaseModel):
    confirm: bool = False
    """The author read the cut and accepts it as the episode list for this film."""


@router.post('/projects/{pid}/segments/approve')
def approve_segments(pid: str, body: SegmentReview):
    """Accept the cut, then let the flow continue into artwork.

    The cut decides both the episodes and which characters and places are worth drawing, so it is
    reviewed before any picture is generated instead of after.
    """
    if not body.confirm: raise HTTPException(422, '请确认已查看全部情节')
    with attempt_lock, Session.begin() as db:
        row = get_work(db, pid)
        if row.data.get('stage') != 'segments_review': raise HTTPException(409, '当前不在情节确认阶段。')
        sid = row.data.get('director_id')
        project = db.get(Record, sid) if sid else None
        segments = ((project.data.get('segments') or {}).get('segments') or []) if project else []
        if not segments: raise HTTPException(409, '还没有可确认的情节切割结果。')
        db.add(Record(id=uid('audit'), kind='audit', data={'target': pid, 'action': 'segments_approved',
            'episodes': [segment['id'] for segment in segments]}))
        row.data = {**row.data, 'segments_approved_at': time.time(), 'stage': 'preparing'}
        row.version += 1
        schedule(db, row, 'preparing')
    return {'queued': True, 'episodes': [segment['id'] for segment in segments]}


@router.post('/projects/{pid}/generate')
def generate(pid: str, body: Confirm):
    creative.paid(body,'llm')
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
        # The first episode is produced first: the rest follow one at a time from 继续.
        first=creative.next_storyboard_unit(row.data['director_id'])
        queue_node(db, row, 'storyboarding', segment_id=first)
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
def publish(pid: str, request: Request, body: creative.Publish):
    # A release is public, so keep a small, explicit creator snapshot on it. Never copy the
    # account uid, OAuth token, payer identity, or private project id into public release data.
    from .zhihu_oauth import current_account
    account = current_account(request)
    creator = None
    if account:
        name = str(account.get('fullname') or '').strip()[:80] or '知乎创作者'
        avatar = account.get('avatar_path')
        creator = {
            'name': name,
            'avatar_path': avatar.strip() if isinstance(avatar, str) and avatar.strip().startswith('https://') else None,
        }
    with Session() as db:
        row = get_work(db, pid)
        # A finished episode can go out before the rest of the film exists.
        if row.data['stage'] not in ('film_review','published','episode_review'): raise HTTPException(409, '成片尚未完成')
        sid = row.data['director_id']
    result = creative.publish(sid, body)
    annotated = None
    with Session.begin() as db:
        row=get_work(db,pid);row.data={**row.data,'stage':'published','release_id':result['id']}
        release=db.get(Record,result['id'])
        if release and release.kind=='reader_release' and creator and release.data.get('creator') != creator:
            release.data={**release.data,'creator':creator};release.version+=1;db.flush()
            result=record_dict(release);annotated=release
    if annotated:
        # Release manifests are immutable by version. Public creator attribution therefore becomes
        # a new manifest revision instead of mutating the already exported v1 file.
        from .video_storage import export_manifest
        export_manifest(annotated)
    return result


class EpisodeContinue(BaseModel):
    confirm_paid: bool = False


@router.post('/projects/{pid}/episodes/continue')
def continue_episodes(pid: str, body: EpisodeContinue):
    """Produce the next episode after the author has seen the finished one."""
    creative.paid(body, 'llm', 'video')
    with attempt_lock, Session.begin() as db:
        row = get_work(db, pid)
        if row.data.get('stage') not in ('episode_review', 'film_review'):
            raise HTTPException(409, '当前不在情节确认阶段。')
        sid = row.data['director_id']
        order, done, run = creative.storyboard_units(sid)
        pending = next((gid for gid in order if gid not in done), None)
        if pending is None:
            pending = creative.next_rendering_unit(sid)
        if pending is None:
            raise HTTPException(409, '全部情节都已生成，可以直接发布。')
        run.data = {**run.data, 'stage': 'references_ready', 'episode_review_pending': None}
        run.version += 1
        db.add(Record(id=uid('audit'), kind='audit', data={'target': pid, 'action': 'episode_continued',
            'segment_id': pending}))
        queue_node(db, row, 'storyboarding', segment_id=pending, reopen=True)
    return {'queued': True, 'segment_id': pending}

def flow(task_id, payload):
    """One persisted scheduling transition per worker invocation; no browser polling dependency."""
    with Session() as db:
        row=db.get(Record,payload['work_id']); data=dict(row.data)
        if payload.get('run_id')!=data.get('run_id'):raise HTTPException(409,'此任务属于已失效的旧轮次，不会更新当前制作。')
        sid=data.get('director_id')
    from .workflows import WORKFLOWS
    # A record without a pinned version is pre-versioning work; the only live line applies to it.
    if data.get('workflow','author-brainstorm-v7') not in WORKFLOWS:
        raise HTTPException(409,'制作工作流版本不可用，请由后台处理')
    if not sid:
        # Recover a director created before a supervisor interruption, rather than enqueue a duplicate.
        request_key=row.id+':'+data['run_id'] if data.get('run_id') else None
        existing=director.projects(data['source_id']) if not request_key else []
        result={'project_id':existing[0]['id']} if existing else director.create(director.Create(source_id=data['source_id'],brief='作者电影视觉方向：'+data['art']+'；剧情气质：'+data['tone']+'。保留原著因果，完成一个起承转合明确的短场景。',confirm_paid=True,request_key=request_key))
        with Session.begin() as db:
            row=db.get(Record,payload['work_id']);row.data={**row.data,'director_id':result['project_id']}
        return False
    with Session() as db:
        project=db.get(Record,sid); origin=db.get(Task,project.data['task_id']); run=db.get(Record,'creative_'+sid)
        tasks=[t for t in db.scalars(select(Task)) if any(t.payload.get(k)==sid for k in ('creative_id','director_id','preproduction_id','project_id'))]
        if any(t.status in creative.BUSY for t in tasks): return False
    if not run:
        if origin.status!='completed': raise HTTPException(409,'原文分析未完成，请在后台处理任务后继续')
        # The cut is reviewed before any artwork is drawn: the author confirms which episodes exist,
        # and the design stage then only draws the characters and places those episodes need.
        if (project.data.get('segments') or {}).get('segments') and not data.get('segments_approved_at'):
            with Session.begin() as db:
                row=db.get(Record,payload['work_id']);row.data={**row.data,'stage':'segments_review'}
            return True
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
    # 基础素材之后的步骤由分镜、漫剧两个独立制作节点推进，作者流程只负责走到素材确认。
    raise HTTPException(409,'制作阶段未完成，请查看后台任务诊断')
