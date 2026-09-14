"""Independent storyboard and video coordinators using reviewed base references."""
import time
from fastapi import HTTPException
from sqlalchemy import select
from .db import Record, Task, Session, uid, task_dict

NODES = {
    'storyboarding': {'kind':'author_storyboard','name':'分镜生成','hint':'直接编写参考图组合分镜、镜头提示词并完成文本预审'},
    'rendering': {'kind':'author_render','name':'漫剧生成','hint':'生成镜头参考图与视频，保存可观看的片段'},
}
ACTIVE_KINDS = {node['kind'] for node in NODES.values()}
KINDS = ACTIVE_KINDS | {'author_composite'}
BUSY = ('queued','running','waiting')
PHASE_STATES = {
    'storyboarding': ('assets_review','fittings_review','trials_review','composites_ready','references_ready','storyboarding','storyboard_ready'),
    'rendering': ('storyboard_ready','samples_review','frames_review','videos_review'),
}


def is_node_task(task, work):
    return task.kind in KINDS and task.payload.get('work_id') == work.id and task.payload.get('run_id') == work.data.get('run_id')


def saved_task_phase(task):
    if task.payload.get('production_phase'):return task.payload['production_phase']
    if task.kind=='image':
        if task.payload.get('asset_kind')=='dressed_character' or task.payload.get('preproduction_id'):return 'compositing'
        if task.payload.get('director_id'):return 'rendering'
    if task.kind=='video':return 'rendering'
    if task.kind=='creative_watch' or (task.kind=='director' and task.payload.get('stage')=='board'):return 'storyboarding'
    return None


def queue_node(db, work, phase, predecessor=None):
    if phase not in NODES:raise HTTPException(409,'未知的制作节点')
    mapping=dict(work.data.get('production_nodes',{}))
    prior=db.get(Task,mapping.get(phase,''))
    if prior and prior.status in (*BUSY,'completed'):return prior
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
    task=Task(id=uid(phase),kind=NODES[phase]['kind'],message=NODES[phase]['name']+'已排队',payload={
        'mode':'live','work_id':work.id,'run_id':work.data.get('run_id'),'phase':phase,
        'title':NODES[phase]['name'],'predecessor':predecessor,'revision_of':prior.id if prior else None,
    })
    db.add(task);mapping[phase]=task.id
    work.data={**work.data,'workflow':'author-brainstorm-v6','stage':phase,'supervisor':task.id,'production_nodes':mapping}
    work.version+=1
    run.data={**run.data,'production_split':True,'direct_reference_inputs':True}
    # Assign existing saved work to its actual responsibility when adopting a legacy run.
    for child in _related(db,work.data['director_id']):
        if child.kind=='image' and (child.payload.get('preproduction_id') or child.payload.get('asset_kind')=='dressed_character'):
            child.payload={**child.payload,'production_phase':'legacy_composition'}
            if child.status=='queued':child.status='cancelled';child.message='新流程已移除定装与试拍，此任务不再提交；记录保留。'
            continue
        if saved_task_phase(child)==phase and (not child.payload.get('production_phase') or child.status in BUSY):
            child.payload={**child.payload,'production_phase':phase,'production_node':task.id}
    return task


def phase_for_saved_state(state):
    if state in ('assets_review','fittings_review','trials_review','composites_ready','references_ready','storyboarding'):return 'storyboarding'
    if state in ('storyboard_ready','samples_review','frames_review','videos_review'):return 'rendering'
    raise HTTPException(409,'当前保存的制作阶段不能接入独立节点，请查看任务记录。')


def node_snapshots(db,work):
    result=[]
    for phase,node in NODES.items():
        task=db.get(Task,work.data.get('production_nodes',{}).get(phase,''))
        result.append({'id':phase,'name':node['name'],'hint':node['hint'],'task':task_dict(task) if task else None})
    return result if work.data.get('production_nodes') else []


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
    if phase=='compositing':
        with Session.begin() as db:
            task,work=_check_owner(db,task_id,owner,payload)
            run=db.get(Record,'creative_'+work.data['director_id'])
            target=phase_for_saved_state(run.data['stage'])
            task.status='superseded';task.message='图像合成节点已移除，改为直接使用基础参考图。'
            next_task=queue_node(db,work,target)
            task.result={**task.result,'handed_off_to':next_task.id}
        return True
    with Session() as db:
        current,work=_check_owner(db,task_id,owner,payload);sid=work.data['director_id']
        run=db.get(Record,'creative_'+sid)
        if not run:raise HTTPException(409,'基础素材尚未准备，无法执行制作节点。')
        state=run.data['stage'];related=[task for task in _related(db,sid) if task.payload.get('production_phase') not in ('legacy_trial','legacy_composition')]
        if state not in PHASE_STATES[phase]:raise HTTPException(409,NODES[phase]['name']+'与当前保存结果不匹配，未继续后续制作。')
        if any(task.status in BUSY for task in related):return False
        replaced={task.payload.get('revision_of') for task in related if task.payload.get('revision_of')}
        failed=[task for task in related if task.payload.get('production_phase')==phase and task.id not in replaced and task.status in ('failed','needs_review')]
        if failed:raise HTTPException(409,NODES[phase]['name']+'暂停：'+failed[0].message)

    advance=lambda stage:lambda pid:creative.advance(pid,creative.Continue(stage=stage,confirm_review=True,confirm_paid=True),automatic=stage!='assets_review')
    if phase=='storyboarding':
        if state in ('assets_review','fittings_review','trials_review','composites_ready'):
            _dispatch(task_id,owner,payload,creative.prepare_reference_inputs)
            state='references_ready'
        if state=='references_ready':_dispatch(task_id,owner,payload,creative.start_storyboard_stage);return False
        if state=='storyboarding':
            with Session() as db:
                run=db.get(Record,'creative_'+sid);watch=db.get(Task,run.data.get('watch',''))
            raise HTTPException(409,'分镜生成暂停：'+(watch.message if watch else '分镜任务记录缺失'))
        return _finish(task_id,owner,payload,'rendering')
    if state=='storyboard_ready':_dispatch(task_id,owner,payload,creative.start_reference_frames);return False
    if state in ('samples_review','frames_review'):_dispatch(task_id,owner,payload,advance(state));return False
    result=creative.production.workspace(sid)
    if not result['shots'] or any(not shot['video_task'] or shot['video_task']['status']!='completed' or not shot['clip'] for shot in result['shots']):
        raise HTTPException(409,'漫剧生成尚未完成，已有片段保留，请处理未完成的镜头。')
    return _finish(task_id,owner,payload)
