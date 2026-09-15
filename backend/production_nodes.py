"""Independent storyboard and video coordinators using reviewed base references."""
import time
from fastapi import HTTPException
from sqlalchemy import select
from .db import Record, Task, Session, uid, task_dict

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


def retry_current_node(db,work):
    """Create a fresh coordinator for the failed current node while retaining prior records."""
    phase=work.data.get('stage')
    if phase not in NODES:raise HTTPException(409,'当前步骤不是可重新运行的制作节点。')
    prior=db.get(Task,work.data.get('production_nodes',{}).get(phase,''))
    sid=work.data.get('director_id','');related=_related(db,sid)
    replaced={task.payload.get('revision_of') for task in related if task.payload.get('revision_of')}
    current=[task for task in related if task.id not in replaced and saved_task_phase(task)==phase
             and task.status not in ('cancelled','superseded')]
    problems=[task for task in current if task.status in PROBLEM]
    if not (prior and prior.status in PROBLEM) and not problems:
        raise HTTPException(409,'当前节点没有可重新运行的错误。')
    if prior and prior.status in ('queued','running'):
        raise HTTPException(409,'当前制作节点仍在运行。')
    if any(task.status in BUSY for task in current):
        raise HTTPException(409,'当前节点仍有子任务在运行，请等待结束后重试。')
    feedback_items=list(dict.fromkeys(task.message for task in [*problems,*([prior] if prior and prior.status in PROBLEM else [])]
                                     if task and task.message))
    if prior and prior.status=='waiting':
        prior.status='needs_review'
        prior.message='子任务已报错，正在建立新的当前节点任务。'
    run=db.get(Record,'creative_'+sid)
    if not run:raise HTTPException(409,'当前制作轮次不存在，不能重新运行节点。')
    if phase=='storyboarding':
        project=db.get(Record,sid)
        raw=(project.data.get('board_diagnostics') or {}).get('raw') if project else None
        chunk=(project.data.get('board_chunk_diagnostics') or {}) if project else {}
        review=(project.data.get('review') or {}) if project else {}
        # A rejected text pre-review is a recoverable failure: the reviewer already named the
        # concrete problems, so they are handed to the storyboard model instead of stopping cold
        # with "请看高级详情".
        review_issues=list(dict.fromkeys(str(issue) for issue in (review.get('issues') or []) if str(issue).strip()))
        review_rejected=review.get('approved') is False and bool(review_issues)
        if review_rejected:
            details=[f'分镜专业预审未通过（{key}：{value}）' for key,value in
                (('连续性',review.get('continuity')),('戏剧逻辑',review.get('dramatic_logic')),
                 ('可剪辑性',review.get('editability')),('制作可行性',review.get('production_feasibility')))
                if isinstance(value,str) and value.strip() and value.strip() not in ('通过','可衔接','成立','可剪辑','待视觉审核')]
            feedback_items.append('上一次分镜专业预审发现了这些问题，本次必须逐条修正：'
                +'；'.join(review_issues[:8])+(('。评审结论：'+'；'.join(details)) if details else ''))
        if chunk.get('errors'):
            detail='；'.join(str(error.get('field'))+'：'+str(error.get('reason')) for error in chunk['errors'][:5])
            feedback_items.append(f"{chunk.get('segment_id') or '片段'} 上一次分镜输出问题：{detail}")
        source_task=next((task for task in problems if task.kind=='director' and task.payload.get('source',{}).get('content')),None)
        if raw is not None and source_task:
            from . import director
            from pydantic import ValidationError
            try:
                saved=director.Board.model_validate(raw)
                saved,_=director.repair_board_causality(saved)
                for issue in director.check_board(saved,source_task.payload['source']['content']):
                    if issue not in feedback_items:feedback_items.append(issue)
            except ValidationError:
                pass
        feedback='；'.join(feedback_items)[:1200]
        # A failure that happened inside one segment must not throw away the segments that
        # already passed validation; only a whole-board failure regenerates everything. A rejected
        # pre-review judges the film as a whole, so every segment is rewritten.
        reuse_valid_chunks=bool(chunk.get('errors')) and not raw and not review_rejected
        for task in problems:
            task.status='superseded';task.message='已由重新运行的分镜节点接替；原错误与输出保留。'
        data={key:value for key,value in run.data.items() if key!='watch'}
        attempts=int(run.data.get('storyboard_review_attempts') or 0)
        if review_rejected:attempts+=1
        run.data={**data,'stage':'references_ready','storyboard_retry_feedback':feedback,
                  'storyboard_review_attempts':attempts,
                  'storyboard_reuse_chunks':reuse_valid_chunks}
        run.version+=1
        replacement=queue_node(db,work,phase)
        replacement.message='分镜生成已重新排队，将参考上次错误重新生成'
        replacements=[]
    else:
        feedback='；'.join(feedback_items)[:1200]
        replacement=queue_node(db,work,phase)
        replacements=[]
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
            db.add(new_task);task.status='superseded';task.message='已由新的重试任务接替；原错误与输出保留。'
            replacements.append(new_task.id)
    db.add(Record(id=uid('audit'),kind='audit',data={'target':work.id,'action':'current_production_node_retried',
        'phase':phase,'previous_node_id':prior.id if prior else None,'replacement_node_id':replacement.id,
        'replacement_task_ids':replacements,'previous_error':feedback}))
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
        if state=='references_ready':_dispatch(task_id,owner,payload,creative.start_storyboard_stage);return False
        if state=='storyboarding':
            with Session() as db:
                run=db.get(Record,'creative_'+sid);watch=db.get(Task,run.data.get('watch',''))
            # Point at the reviewer's own words instead of asking the operator to dig for them.
            project=db.get(Record,sid)
            issues=[str(issue).strip() for issue in ((project.data.get('review') or {}).get('issues') or []) if str(issue).strip()]
            detail=('；'.join(issues[:5])) if issues else (watch.message if watch else '分镜任务记录缺失')
            run_row=db.get(Record,'creative_'+sid)
            attempts=int((run_row.data.get('storyboard_review_attempts') if run_row else 0) or 0)
            if attempts<creative.REVIEW_RETRY_ATTEMPTS:
                detail+=f'（预审重做上限 {creative.REVIEW_RETRY_ATTEMPTS} 次，下一次是第 {attempts+1} 次）'
            else:
                detail+='（已重做满次数，继续当前节点会带着这些问题往下做，不会一直停在这里）'
            raise HTTPException(409,'分镜生成暂停：'+detail)
        return _finish(task_id,owner,payload,'rendering')
    if state=='storyboard_ready':
        # 分镜通过文本预审后，直接用已审核的项目参考图提交每个镜头的视频。
        _dispatch(task_id,owner,payload,creative.start_reference_videos);return False
    result=creative.production.workspace(sid)
    if not result['shots'] or any(not shot['video_task'] or shot['video_task']['status']!='completed' or not shot['clip'] for shot in result['shots']):
        raise HTTPException(409,'漫剧生成尚未完成，已有片段保留，请处理未完成的镜头。')
    return _finish(task_id,owner,payload)
