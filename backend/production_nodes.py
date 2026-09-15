"""Independent storyboard and video coordinators using reviewed base references."""
import time
from fastapi import HTTPException
from sqlalchemy import select
from .db import DATA, Record, Task, Session, uid, task_dict

NODES = {
    'storyboarding': {'kind':'author_storyboard','name':'分镜生成','hint':'直接编写参考图组合分镜、镜头提示词并完成文本预审'},
    'rendering': {'kind':'author_render','name':'漫剧生成','hint':'用已审核的项目参考图直接生成视频，保存可观看的片段'},
}
ACTIVE_KINDS = {node['kind'] for node in NODES.values()}
KINDS = ACTIVE_KINDS
BUSY = ('queued','running','waiting')
PROBLEM = ('failed','needs_review')
PHASE_STATES = {
    'storyboarding': ('assets_review','references_ready','storyboarding','storyboard_ready'),
    'rendering': ('storyboard_ready','videos_review'),
}


def is_node_task(task, work):
    return task.kind in KINDS and task.payload.get('work_id') == work.id and task.payload.get('run_id') == work.data.get('run_id')


def adopt_current_payer(task):
    """Let unfinished work change who pays for it.

    A task records the payer it was created with, and the worker bills that payer. Without this, a
    visitor who switched to their own API keys after a wallet or shared-key problem kept being
    billed by the old payer and saw the same refusal forever.
    """
    from .model_access import current_access_id

    access_id=current_access_id()
    if not access_id or task.session_id==access_id:return False
    # Never move work that already reached a provider: its submission is tied to the old account.
    if (task.result or {}).get('provider_id'):return False
    task.session_id=access_id
    return True


def saved_task_phase(task):
    if task.payload.get('production_phase'):return task.payload['production_phase']
    if task.kind=='image':return None  # 人物身份图、服装图与场景图属于基础素材，不属于任何制作节点。
    if task.kind=='video':return 'rendering'
    if task.kind=='creative_watch' or (task.kind=='director' and task.payload.get('stage')=='board'):return 'storyboarding'
    return None


def queue_node(db, work, phase, predecessor=None):
    if phase not in NODES:raise HTTPException(409,'未知的制作节点')
    mapping=dict(work.data.get('production_nodes',{}))
    prior=db.get(Task,mapping.get(phase,''))
    if prior and prior.status in (*BUSY,'completed'):
        if prior.status!='completed':adopt_current_payer(prior)
        return prior
    run=db.get(Record,'creative_'+work.data.get('director_id',''))
    if not run or run.data.get('stage') not in PHASE_STATES[phase]:
        raise HTTPException(409,'本节点需要的上游结果尚未就绪。')
    if predecessor:
        previous=db.get(Task,predecessor)
        expected=list(NODES)[list(NODES).index(phase)-1] if phase!='storyboarding' else None
        if not previous or not is_node_task(previous,work) or previous.status!='completed' or previous.payload.get('phase')!=expected:
            raise HTTPException(409,'上游制作节点尚未完成，不能继续。')
    if prior:
        prior.status='superseded'
        prior.message='此节点已由新的恢复任务接替，原记录与结果保留。'
    task=Task(id=uid(phase),kind=NODES[phase]['kind'],status='queued',message=NODES[phase]['name']+'已排队',payload={
        'mode':'live','work_id':work.id,'run_id':work.data.get('run_id'),'phase':phase,
        'title':NODES[phase]['name'],'predecessor':predecessor,'revision_of':prior.id if prior else None,
    })
    db.add(task);mapping[phase]=task.id
    work.data={**work.data,'workflow':'author-brainstorm-v7','stage':phase,'supervisor':task.id,'production_nodes':mapping}
    work.version+=1
    run.data={**run.data,'production_split':True,'direct_reference_inputs':True}
    # Saved work of this phase is pointed at the node that now owns it, so a resumed or retried
    # node finds the results it is waiting for.
    for child in _related(db,work.data['director_id']):
        if saved_task_phase(child)==phase and (not child.payload.get('production_phase') or child.status in BUSY):
            child.payload={**child.payload,'production_phase':phase,'production_node':task.id}
    return task


def node_snapshots(db,work):
    result=[]
    for phase,node in NODES.items():
        task=db.get(Task,work.data.get('production_nodes',{}).get(phase,''))
        result.append({'id':phase,'name':node['name'],'hint':node['hint'],'task':task_dict(task) if task else None})
    return result if work.data.get('production_nodes') else []


def stopped_stage_media(db,work,phase=None):
    """The image and video items of one stage that stopped and still need a new version.

    A stage is not one task: it owns every picture and clip it produced. Asking those items
    separately is what keeps a stopped film from being repaired one file at a time.
    """
    phase=phase or work.data.get('stage')
    if phase not in NODES:return []
    sid=work.data.get('director_id','')
    related=_related(db,sid) if sid else []
    replaced={task.payload.get('revision_of') for task in related if task.payload.get('revision_of')}
    return [task for task in related if task.id not in replaced and saved_task_phase(task)==phase
            and task.kind in ('image','video') and task.status in PROBLEM]


def failure_summary(failed,limit=800,listed=6):
    """Name every stopped item of one stage, not only whichever failed first."""
    shown=failed[:listed]
    labels=[f"{task.payload.get('title') or task.payload.get('shot_id') or task.payload.get('label') or task.kind}：{task.message}"
            for task in shown]
    if len(failed)>len(shown):labels.append(f'另有 {len(failed)-len(shown)} 项未列出')
    return '；'.join(dict.fromkeys(labels))[:limit]


def stage_payer(db,work):
    """The payer this run is spending, so a requeue never loses it.

    A node queued from a browser request carries the access session that request used. A node
    requeued by the worker has no browser in scope: without this the replacement was created with
    an empty payer and refused every call with "请先用知乎账号登录领取算力豆", even for a browser
    that had already attached its own key.
    """
    from .model_access import current_access_id

    scoped=current_access_id()
    if scoped:return scoped
    for phase in NODES:
        node=db.get(Task,work.data.get('production_nodes',{}).get(phase,'') or '')
        if node and node.session_id:return node.session_id
    for task in _related(db,work.data.get('director_id','')):
        if task.session_id:return task.session_id
    return ''


def blocking_feedback(db,work,project=None,problems=()):
    """Only the errors a rewrite can act on, in the order the model should read them.

    Structural checks are decidable by code — shot numbers, source references, bound assets, the
    schema shape — so they are handed back as instructions for the next attempt. The text
    pre-review's creative opinions are not: sending them back as "必须逐条修正" made the storyboard
    model argue with the reviewer instead of producing a board, and every attempt rewrote the whole
    film. Those opinions are recorded for the author instead (see ``review_notes``).
    """
    sid=work.data.get('director_id','')
    if project is None:project=db.get(Record,sid) if sid else None
    # The node's own failure messages are written by the validators, so they are safe to hand back.
    # Anything the reviewer said is excluded even if it reached a message on the way here.
    items=[task.message for task in problems
           if task.message and '分镜专业预审' not in task.message]
    if project:
        chunk=project.data.get('board_chunk_diagnostics') or {}
        if chunk.get('errors'):
            detail='；'.join(f"{error.get('field')}：{error.get('reason')}" for error in chunk['errors'][:6])
            items.append(f"{chunk.get('segment_id') or '本片段'} 上一次输出未通过结构检查，必须修正：{detail}")
        raw=(project.data.get('board_diagnostics') or {}).get('raw')
        if raw is not None:
            from . import director
            from pydantic import ValidationError
            source=next((task.payload.get('source',{}).get('content','') for task in _related(db,sid)
                         if task.kind=='director' and task.payload.get('source',{}).get('content')),'')
            try:
                saved=director.Board.model_validate(raw)
                saved,_=director.repair_board_causality(saved)
                if source:items.extend(director.check_board(saved,source))
            except ValidationError:
                pass
    return list(dict.fromkeys(item for item in items if item))[:12]


def stopped_node(db,work,phase=None):
    """The coordinator of one stage plus the media it owns that still needs a new version."""
    phase=phase or work.data.get('stage')
    if phase not in NODES:return None,[],[]
    prior=db.get(Task,work.data.get('production_nodes',{}).get(phase,'') or '')
    sid=work.data.get('director_id','');related=_related(db,sid)
    replaced={task.payload.get('revision_of') for task in related if task.payload.get('revision_of')}
    current=[task for task in related if task.id not in replaced and saved_task_phase(task)==phase
             and task.status not in ('cancelled','superseded')]
    problems=[task for task in current if task.status in PROBLEM]
    if prior and prior.status in PROBLEM and prior not in problems:problems.append(prior)
    return prior,current,problems


def retry_node(db,work):
    """Run the stopped node again with the errors it should fix, and change nothing else.

    A retry keeps the node's inputs, its saved results and the records of earlier attempts. Work
    that already reached a provider is re-checked rather than submitted a second time. Throwing the
    node away and starting over is ``redo_node``; the two are deliberately separate, because mixing
    them meant "继续/重试" silently destroyed saved work.
    """
    phase=work.data.get('stage')
    if phase not in NODES:raise HTTPException(409,'当前步骤不是可重新运行的制作节点。')
    prior,current,problems=stopped_node(db,work,phase)
    if not problems:
        raise HTTPException(409,'当前节点没有可重新运行的错误。')
    if prior and prior.status in ('queued','running'):
        raise HTTPException(409,'当前制作节点仍在运行。')
    if any(task.status in BUSY for task in current):
        raise HTTPException(409,'当前节点仍有子任务在运行，请等待结束后重试。')
    if prior and prior.status=='waiting':
        prior.status='needs_review'
        prior.message='子任务已报错，正在建立新的当前节点任务。'
    sid=work.data.get('director_id','')
    run=db.get(Record,'creative_'+sid)
    if not run:raise HTTPException(409,'当前制作轮次不存在，不能重新运行节点。')
    feedback='；'.join(blocking_feedback(db,work,problems=problems))[:1200]
    if phase=='storyboarding':
        project=db.get(Record,sid)
        chunk=(project.data.get('board_chunk_diagnostics') or {}) if project else {}
        # Only the segments that failed are rewritten; a segment that already passed every check is
        # kept, so a retry never pays for the whole film a second time.
        reuse_valid_chunks=bool(chunk.get('errors'))
        for task in problems:
            task.status='superseded';task.message='已由重新运行的分镜节点接替；原错误与输出保留。'
        data={key:value for key,value in run.data.items() if key!='watch'}
        run.data={**data,'stage':'references_ready','storyboard_retry_feedback':feedback,
                  'storyboard_reuse_chunks':reuse_valid_chunks}
        run.version+=1
        replacement=queue_node(db,work,phase)
        replacement.message='分镜生成已重新排队，只带结构错误重做未通过的片段'
        replacements=[]
    else:
        replacement=queue_node(db,work,phase)
        replacements=[]
        payer=stage_payer(db,work)
        for task in problems:
            if task.kind not in ('image','video'):
                task.status='superseded';task.message='已由重新运行的漫剧节点接替；原错误与输出保留。'
                continue
            if task.status=='needs_review' and task.result.get('provider_id'):
                task.status='queued';task.lease=0;task.owner=''
                task.payload={**task.payload,'production_phase':phase,'production_node':replacement.id}
                task.message='正在重新核实已提交的供应商任务，不重复提交'
                replacements.append(task.id);continue
            new_task=Task(id=uid(task.kind),kind=task.kind,
                message='失败任务已重新排队',payload={**task.payload,'production_phase':phase,
                    'production_node':replacement.id,'revision_of':task.id})
            # The rerun is billed to the same payer as the node it replaces, never to nobody.
            if payer:new_task.session_id=payer
            db.add(new_task);task.status='superseded';task.message='已由新的重试任务接替；原错误与输出保留。'
            replacements.append(new_task.id)
    db.add(Record(id=uid('audit'),kind='audit',data={'target':work.id,'action':'current_production_node_retried',
        'phase':phase,'previous_node_id':prior.id if prior else None,'replacement_node_id':replacement.id,
        'replacement_task_ids':replacements,'previous_error':feedback}))
    return replacement


def _delete_task_outputs(db,task):
    """Remove the media one task produced, so a redo does not leave orphaned clips behind."""
    result=task.result or {}
    clip=db.get(Record,result.get('clip_id','') or '')
    if clip:
        artifact=db.get(Record,clip.data.get('artifact_id','') or '')
        if artifact:
            for field in ('source','playback'):
                media=(artifact.data.get(field) or {}).get('media')
                if isinstance(media,str) and media.startswith('/media/'):
                    path=(DATA/'media'/media.removeprefix('/media/')).resolve()
                    if path.is_relative_to((DATA/'media').resolve()):path.unlink(missing_ok=True)
            db.delete(artifact)
        for use in db.scalars(select(Record).where(Record.kind=='clip_use')):
            if use.data.get('clip_id')==clip.id:db.delete(use)
        media=clip.data.get('media')
        if isinstance(media,str) and media.startswith('/media/'):
            path=(DATA/'media'/media.removeprefix('/media/')).resolve()
            if path.is_relative_to((DATA/'media').resolve()):path.unlink(missing_ok=True)
        db.delete(clip)
    asset=db.get(Record,result.get('asset_id','') or '')
    if asset:db.delete(asset)


def redo_node(db,work):
    """Delete this node and everything after it, then start the node over from its entry state.

    A redo is the explicit "throw it away and begin again" action. The node's own products — the
    board, the review, the compiled prompts, the clips and their artifacts — are deleted instead of
    archived, and the downstream node loses its work too, so the node runs against exactly the
    upstream inputs it had the first time and nothing from the discarded attempt can leak into the
    new one.
    """
    phase=work.data.get('stage')
    if phase not in NODES:raise HTTPException(409,'当前步骤不是可重做的制作节点。')
    prior,current,problems=stopped_node(db,work,phase)
    if any(task.status in BUSY for task in current) or (prior and prior.status in ('queued','running')):
        raise HTTPException(409,'当前制作节点仍在运行，请等待结束后重做。')
    sid=work.data.get('director_id','')
    run=db.get(Record,'creative_'+sid)
    project=db.get(Record,sid)
    if not run or not project:raise HTTPException(409,'当前制作轮次不存在，不能重做节点。')
    downstream=('rendering',) if phase=='storyboarding' else ()
    for name in (phase,*downstream):
        node=db.get(Task,work.data.get('production_nodes',{}).get(name,'') or '')
        if node and node.status in BUSY:raise HTTPException(409,f'{NODES[name]["name"]}仍在运行，请等待结束后重做。')
    # Everything this node and the nodes after it produced.
    discard=list(current)
    if prior:discard.append(prior)
    for name in downstream:
        _,_,problems_after=stopped_node(db,work,name)
        discard.extend(problems_after)
        mapping=work.data.get('production_nodes',{})
        discard.extend(task for task in _related(db,sid)
                       if task.id==mapping.get(name) and task not in discard)
    for task in discard:
        _delete_task_outputs(db,task)
        db.delete(task)
    # What the node wrote into the run and the director project.
    data={key:value for key,value in run.data.items()
          if key not in ('watch','storyboard_retry_feedback','storyboard_reuse_chunks',
                         'storyboard_review_attempts','storyboard_review_degraded','storyboard_review_notes')}
    run.data={**data,'stage':'references_ready' if phase=='storyboarding' else 'storyboard_ready'}
    run.version+=1
    if phase=='storyboarding':
        project.data={key:value for key,value in project.data.items()
                      if key not in ('board','review','board_diagnostics','board_chunk_diagnostics',
                                     'board_progress','board_repairs','board_recovery','board_history',
                                     'prompts','preproduction_stamp')}
        project.data={**project.data,'status':'awaiting_preproduction'}
        project.version+=1
    mapping={key:value for key,value in work.data.get('production_nodes',{}).items() if key not in (phase,*downstream)}
    work.data={**work.data,'production_nodes':mapping}
    replacement=queue_node(db,work,phase)
    replacement.message=f'{NODES[phase]["name"]}已重新开始，本轮之前的记录已删除'
    db.add(Record(id=uid('audit'),kind='audit',data={'target':work.id,'action':'current_production_node_redone',
        'phase':phase,'discarded_task_ids':[task.id for task in discard],
        'replacement_node_id':replacement.id}))
    return replacement


def _related(db,sid):
    return [task for task in db.scalars(select(Task).order_by(Task.created)) if any(task.payload.get(key)==sid for key in ('creative_id','director_id','preproduction_id','project_id'))]


def _check_owner(db,task_id,owner,payload):
    task=db.get(Task,task_id);work=db.get(Record,payload['work_id'])
    if not task or not work or task.status!='running' or task.owner!=owner or work.data.get('run_id')!=payload.get('run_id') or work.data.get('supervisor')!=task_id:
        raise HTTPException(409,'此制作节点已经停止或被新轮次替代。')
    return task,work


def _dispatch(task_id,owner,payload,action):
    with Session() as db:
        _,work=_check_owner(db,task_id,owner,payload);sid=work.data['director_id']
        before={task.id for task in _related(db,sid)}
    action(sid)
    with Session.begin() as db:
        current,work=_check_owner(db,task_id,owner,payload)
        children=[]
        for task in _related(db,sid):
            if task.id not in before:
                task.payload={**task.payload,'production_phase':payload['phase'],'production_node':task_id}
                children.append(task.id)
        current.result={**current.result,'children':list(dict.fromkeys([*current.result.get('children',[]),*children]))}


def _finish(task_id,owner,payload,next_phase=None):
    with Session.begin() as db:
        task,work=_check_owner(db,task_id,owner,payload)
        sid=work.data['director_id'];run=db.get(Record,'creative_'+sid);project=db.get(Record,sid)
        phase=payload['phase']
        if phase=='storyboarding':outputs=[shot['id'] for shot in project.data.get('board',{}).get('shots',[])]
        else:
            from .production import workspace
            outputs=[shot['video_task']['id'] for shot in workspace(sid)['shots']]
        task.status='completed';task.progress=100;task.message=NODES[phase]['name']+'已完成，结果已保存'
        task.result={**task.result,'director_id':sid,'output_ids':list(dict.fromkeys(outputs)),'finished_at':time.time()}
        if next_phase:queue_node(db,work,next_phase,predecessor=task.id)
        else:work.data={**work.data,'stage':'film_review'};work.version+=1
    return True


def run_node(task_id,payload,owner):
    from . import creative
    phase=payload['phase']
    with Session() as db:
        current,work=_check_owner(db,task_id,owner,payload);sid=work.data['director_id']
        run=db.get(Record,'creative_'+sid)
        if not run:raise HTTPException(409,'基础素材尚未准备，无法执行制作节点。')
        state=run.data['stage'];related=_related(db,sid)
        if state not in PHASE_STATES[phase]:raise HTTPException(409,NODES[phase]['name']+'与当前保存结果不匹配，未继续后续制作。')
        if any(task.status in BUSY for task in related):return False
        replaced={task.payload.get('revision_of') for task in related if task.payload.get('revision_of')}
        failed=[task for task in related if task.payload.get('production_phase')==phase and task.id not in replaced and task.status in ('failed','needs_review')]
        # Every stopped picture and clip is named, so the operator sees the whole stage instead of
        # one error while other items quietly wait for a decision of their own.
        if failed:raise HTTPException(409,NODES[phase]['name']+'暂停：'+failure_summary(failed))

    if phase=='storyboarding':
        if state=='assets_review':
            _dispatch(task_id,owner,payload,creative.prepare_reference_inputs)
            state='references_ready'
        if state=='references_ready':
            pending=creative.next_storyboard_unit(sid)
            if pending is None:raise HTTPException(409,'这部作品没有可制作的情节，请重新查看切割结果。')
            _dispatch(task_id,owner,payload,lambda pid:creative.start_storyboard_stage(pid,pending))
            return False
        if state=='storyboarding':
            pending=creative.next_storyboard_unit(sid)
            if pending is None:
                # Every episode has a board: lock the film and hand it to the rendering node.
                creative.finish_storyboard(sid)
                return _finish(task_id,owner,payload,'rendering')
            with Session() as db:
                run=db.get(Record,'creative_'+sid);watch=db.get(Task,run.data.get('watch',''))
                # The episode that was in flight did not produce a board, so point at the reason it
                # stopped: a structural error the model can act on, otherwise the task's own message.
                detail='；'.join(blocking_feedback(db,work,problems=[watch] if watch else [])) or '分镜任务记录缺失'
            raise HTTPException(409,f'分镜生成暂停（{pending}）：'+detail)
    if state in ('storyboard_ready','videos_review'):
        pending=creative.next_rendering_unit(sid)
        if pending is None:
            result=creative.production.workspace(sid)
            if not result['shots'] or any(not shot['video_task'] or shot['video_task']['status']!='completed' or not shot['clip'] for shot in result['shots']):
                raise HTTPException(409,'漫剧生成尚未完成，已有片段保留，请处理未完成的镜头。')
            return _finish(task_id,owner,payload)
        # Episodes are rendered in order; the next one is submitted only once this one is finished.
        _dispatch(task_id,owner,payload,lambda pid:creative.start_reference_videos(pid,pending))
        return False
    raise HTTPException(409,NODES[phase]['name']+'与当前保存结果不匹配，未继续后续制作。')
