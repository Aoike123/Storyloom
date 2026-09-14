from fastapi import HTTPException
import time
import threading
import httpx
from sqlalchemy import select, update, or_, and_
from .db import Record,Task,Session,uid,init_db,DATA
from .providers import submit_video,poll_video,ProviderError
from .image_provider import generate_image,generate_from_references,save_image,local_frame_data
from .video_storage import register_artifact, attach_artifact, verify_file
from .task_activity import media_activity
from .production_nodes import KINDS as PRODUCTION_KINDS

def claim(owner):
    now=time.time()
    with Session.begin() as db:
        row=db.scalars(select(Task).where(or_(Task.status=='queued',and_(Task.status.in_(['running','waiting']),Task.lease<now)))
                       .order_by(Task.created).limit(1)).first()
        if not row:return None
        if row.status=='running' and (row.kind=='author_flow' or row.kind in PRODUCTION_KINDS or (row.kind in ['video','image','director','art_design','creative_revision','creative_watch','author_styles'] and row.payload.get('mode')=='live' and not row.result.get('provider_id'))):
            # Paid submission may have succeeded before its response was recorded.
            changed=db.execute(update(Task).where(Task.id==row.id,Task.lease==row.lease,Task.status=='running')
                .values(status='needs_review',message='执行中断且调用结果不确定，请核实供应商记录后处理。'))
            return None
        values={'status':'running','owner':owner,'lease':now+90,'attempts':row.attempts+1}
        if row.status=='queued':values['message']='任务已开始，正在准备处理'
        changed=db.execute(update(Task).where(Task.id==row.id,Task.status==row.status,Task.lease==row.lease).values(**values))
        return row.id if changed.rowcount else None

def patch(task_id,owner,**values):
    with Session.begin() as db:
        return db.execute(update(Task).where(Task.id==task_id,Task.owner==owner,Task.status=='running').values(**values)).rowcount

def heartbeat(task_id,owner,stop):
    while not stop.wait(20):
        if not patch(task_id,owner,lease=time.time()+90):break
        with Session.begin() as db:
            row=db.get(Record,'worker_heartbeat')
            if row:row.data={'at':time.time()}

def save_video_result(task_id,owner,p,result,source):
    media_activity(task_id,owner,'checking','视频已接收，正在检查画面文件、时长与播放格式')
    clip_id=f'{task_id}_clip'
    with Session.begin() as db:
        current=db.get(Task,task_id)
        if current.owner!=owner or current.status!='running':return
        artifact=register_artifact(db,clip_id,source,{
            'origin':'generation','task_id':task_id,'provider_id':result['provider_id'],
            'generation':result.get('generation',{'snapshot_status':'legacy_unverified'}),
            'input_snapshot':p.get('input_snapshot'),
        })
        clip=db.get(Record,clip_id)
        if not clip:
            data={'title':p.get('title','真实生成片段'),'demo':False,'status':'pending','narration':p['prompt'],
                  'events':[],'entry_state':{},'assets':[],'annotated':False,'source_task':task_id,
                  'director_id':p.get('director_id'),'director_version':p.get('director_version'),
                  'shot_id':p.get('shot_id'),'input_snapshot':p.get('input_snapshot')}
            clip=Record(id=clip_id,kind='clip',data=data);db.add(clip)
        attach_artifact(clip,artifact)
        duration=artifact.data['source']['properties']['duration']
        saved={**result,'clip_id':clip_id,'artifact_id':artifact.id,'media':clip.data['media'],'storage_schema_version':1}
        current.result=saved
    from .provider_usage import finish_video
    finish_video(task_id,{'duration':duration})
    patch(task_id,owner,status='completed',progress=100,message='独立视频及素材来源已保存，等待审片与选取区间',result=saved)


def run_task(task_id,owner):
    with Session() as db:
        row=db.get(Task,task_id);kind=row.kind;p=dict(row.payload);result=dict(row.result)
    if kind=='author_styles':
        from .authors import recommend_styles
        recommend_styles(task_id,p,owner)
        patch(task_id,owner,status='completed',progress=100,message='风格建议已生成')
    elif kind in PRODUCTION_KINDS:
        from .production_nodes import run_node,NODES
        done=run_node(task_id,p,owner)
        if not done:patch(task_id,owner,status='waiting',lease=time.time()+5,message=NODES[p['phase']]['name']+'进行中，等待本节点任务完成')
    elif kind=='author_flow':
        from .authors import flow
        done=flow(task_id,p)
        patch(task_id,owner,status='completed' if done else 'waiting',lease=time.time()+5,progress=100 if done else 30,message='本轮完成，等待作者确认' if done else '后台正在自动调度，请查看各项真实任务进度')
    elif kind in ('art_design','creative_revision','creative_watch'):
        from .creative import run_design,run_revision,run_watch
        if kind=='art_design':run_design(task_id,p)
        elif kind=='creative_revision':run_revision(task_id,p)
        elif not run_watch(task_id,p):
            patch(task_id,owner,status='waiting',lease=time.time()+10,message='系统正在设计分镜并进行专业预审',progress=40)
            return
        patch(task_id,owner,status='completed',progress=100,message='完整提示词已按意见重写，图片或视频正在重新生成。' if kind=='creative_revision' else '本阶段已完成，请查看画面或后续任务')
    elif kind=='director':
        from .director import run_director
        if p.get('stage')=='board':
            from .preproduction import ready
            with Session() as db:
                if ready(db,p['project_id'])['stamp']!=p['preproduction']['stamp']:raise ProviderError('选角搭景已变化，分镜任务停止。')
        def save_stage(key,value,progress):
            with Session.begin() as db:
                current=db.get(Task,task_id)
                if current.status!='running' or current.owner!=owner:raise ProviderError('导演任务已停止。')
                project=db.get(Record,p['project_id'])
                project.data={**project.data,key:value};project.version+=1
                message=value if key=='phase' else {'treatment':'导演阐述已保存','treatment_diagnostics':'导演阐述输出问题已保存，正在自动重试','board':'分镜已保存','board_plan':'专业镜头计划已保存，正在编写生成提示词','prompt_diagnostics':'生成提示词问题已保存，正在自动重试','board_diagnostics':'分镜输出问题已保存，正在自动重试','board_repairs':'分镜素材绑定已按唯一场景名称校正'}[key]
                project.data={**project.data,'events':[*project.data.get('events',[]),{'at':time.time(),'message':message}]}
                current.progress=progress;current.message=message
        review=run_director(p,task_id,save_stage)
        with Session.begin() as db:
            current=db.get(Task,task_id)
            if current.status!='running' or current.owner!=owner:return
            project=db.get(Record,p['project_id'])
            project.data={**project.data,'review':review,'status':'awaiting_preproduction' if p.get('stage')=='treatment' else 'pending_review'};project.version+=1
            current.status='completed';current.progress=100;current.message='导演阐述已完成，请选角、搭影棚并审核定装试拍' if p.get('stage')=='treatment' else '脑洞导演方案已生成，等待人工审核'
            current.result={**current.result,'project_id':project.id}
    elif kind=='image':
        from .asset_workflow import validate_dependencies
        with Session() as db:validate_dependencies(db,p)
        if p.get('preproduction_id'):
            from .preproduction import snapshot,project
            with Session() as db:
                project(db,p['preproduction_id'])
                if snapshot(db,p['preproduction_id'])[1]!=p['preproduction_stamp']:raise ProviderError('选角或搭景已变化，请重新试拍。')
        if p.get('director_id'):
            from .consistency import ready
            with Session() as db:
                _,token,_=ready(db,p['director_id'],p['shot_id'])
                if token!=p.get('consistency_stamp'):raise ProviderError('视觉版本已更新，请重新提交当前镜头。')
        media_activity(task_id,owner,'generating','参考设定已准备，正在等待模型返回画面')
        url=generate_from_references(p['prompt'],task_id,references=p['reference_media'],model=p['image_model'],steps=p.get('image_inference_steps',50)) if p.get('reference_media') else generate_image(p['prompt'],task_id)
        media_activity(task_id,owner,'downloading','模型已返回图片，正在接收并检查画面文件')
        media=save_image(url,task_id)
        media_activity(task_id,owner,'saving','图片检查通过，正在保存到本作品素材')
        asset_id=f'{task_id}_asset'
        with Session.begin() as db:
            current=db.get(Task,task_id)
            if current.owner!=owner or current.status!='running':return
            validate_dependencies(db,p)
            if not db.get(Record,asset_id):
                db.add(Record(id=asset_id,kind='asset',data={'name':p['title'],'type':p.get('asset_role','scene'),'description':p['prompt'],
                    'asset_kind':p.get('asset_kind'),'character_key':p.get('character_key'),'costume_key':p.get('costume_key'),
                    'identity_asset_id':p.get('identity_asset_id'),'costume_asset_id':p.get('costume_asset_id'),
                    'asset_dependencies':p.get('asset_dependencies',[]),
                    'status':'pending','palette':'ink','media':media,'demo':False,'source_task':task_id,'mutable':['description'],
                    'consistency_stamp':p.get('consistency_stamp'),'reference_assets':p.get('reference_assets'),'director_id':p.get('director_id'),'director_version':p.get('director_version'),'shot_id':p.get('shot_id'),'motion_prompt':p.get('motion_prompt'),
                    'generation_seconds':p.get('generation_seconds'),'edit_seconds':p.get('edit_seconds')}))
            from .image_messages import completed_message
            current.status='completed';current.progress=100;current.message=completed_message(p)
            current.result={**current.result,'asset_id':asset_id,'media':media}
    elif kind=='video':
        if not result.get('provider_id'):
            if p.get('director_id'):
                from .consistency import ready
                with Session() as db:
                    _,token,_=ready(db,p['director_id'],batch=True)
                    a=db.get(Record,p.get('asset_id',''))
                    if token!=p.get('consistency_stamp') or not a or a.version!=p.get('asset_version') or a.data.get('status')!='approved':raise ProviderError('视觉设定或镜头参考图已变化，请重新审核。')
            for binding in (p.get('input_snapshot') or {}).get('asset_bindings',[]):
                if binding.get('file'):verify_file(binding['file'])
            from .providers import model_config, endpoint
            video_config=model_config()
            result={**result,'generation':{'provider':video_config.get('VIDEO_PROVIDER','ark'),
                'model':video_config.get('VIDEO_MODEL',''),'endpoint':endpoint('video',video_config),
                'requested_duration':p.get('generation_seconds') or video_config.get('VIDEO_DURATION','8'),
                'requested_resolution':video_config.get('VIDEO_RESOLUTION','768P')}}
            patch(task_id,owner,progress=10,message='正在向视频供应商提交；超时不自动重发',result=result)
            media_activity(task_id,owner,'submitting','参考图片与镜头说明已准备，正在提交视频制作')
            reference_media=p.get('reference_media') or ([p['frame_media']] if p.get('frame_media') else [])
            if reference_media:
                provider_id=submit_video(p['prompt'],task_id,reference_images=[local_frame_data(media) for media in reference_media],duration_seconds=p.get('generation_seconds'),config=video_config)
            else: provider_id=submit_video(p['prompt'],task_id,p.get('image_url'),config=video_config)
            result={**result,'provider_id':provider_id,'submitted':time.time()}
            with Session.begin() as db:
                db.execute(update(Task).where(Task.id==task_id,Task.owner==owner).values(result=result))
                current=db.get(Task,task_id)
                if current.status!='running':return
        # Resume ingestion from a completed local source without a second paid
        # submission, even when the provider's download URL has expired.
        clip_id=f'{task_id}_clip'
        staging=DATA/'staging';staging.mkdir(exist_ok=True)
        tmp=staging/f'{task_id}.download'
        source=next((DATA/'media'/'clips'/clip_id).glob('source.*'),None)
        if not source and result.get('download_complete') and tmp.is_file():source=tmp
        if source:
            save_video_result(task_id,owner,p,result,source)
            return
        generation=result.get('generation',{})
        if generation.get('endpoint'):
            from .providers import model_config, endpoint
            current_config=model_config()
            if generation['endpoint']!=endpoint('video',current_config) or generation['provider']!=current_config.get('VIDEO_PROVIDER','ark'):
                raise ProviderError('视频供应商配置已切换，已保留原任务编号；请恢复对应供应商配置后继续查询。')
        media_activity(task_id,owner,'generating','视频任务已受理，正在查询制作结果')
        status,url=poll_video(result['provider_id'])
        if status=='succeeded' and url:
            # URL comes from configured provider, never from arbitrary reader input.
            if not url.startswith('https://'):raise ProviderError('供应商返回了不受支持的下载地址。')
            media_activity(task_id,owner,'downloading','视频制作已完成，正在接收片段')
            with httpx.stream('GET',url,follow_redirects=True,timeout=90) as r:
                r.raise_for_status();total=0
                with tmp.open('wb') as f:
                    for chunk in r.iter_bytes():
                        total+=len(chunk)
                        if total>250*1024*1024:raise ProviderError('视频超过第一版 250MB 下载限制。')
                        f.write(chunk)
            result={**result,'download_complete':True}
            if not patch(task_id,owner,progress=75,message='视频已下载，正在检查并登记独立素材',result=result):return
            save_video_result(task_id,owner,p,result,tmp)
            tmp.unlink(missing_ok=True)
        elif status in ['failed','cancelled','expired']:
            patch(task_id,owner,status='failed',message=f'供应商任务状态：{status}',result=result)
        elif time.time()-result['submitted']>3600:
            patch(task_id,owner,status='needs_review',message='等待超过一小时，已保留任务编号供核实',result=result)
        else:
            patch(task_id,owner,status='waiting',progress=45,message='供应商正在生成，已保存任务编号',result=result,lease=time.time()+10)
    else:raise ProviderError('未知任务类型')

def process_one(owner):
    task_id=claim(owner)
    if not task_id:return False
    with Session() as db:access_id=db.get(Task,task_id).session_id
    stop=threading.Event();thread=threading.Thread(target=heartbeat,args=(task_id,owner,stop),daemon=True);thread.start()
    try:
        from .model_access import access_scope
        with access_scope(access_id):run_task(task_id,owner)
    except HTTPException as exc:patch(task_id,owner,status='needs_review',message=str(exc.detail)[:900])
    except ProviderError as exc:patch(task_id,owner,status='needs_review',message=str(exc)[:900])
    except Exception as exc:
        # Do not leak provider response bodies, signed URLs, credentials, or local paths.
        patch(task_id,owner,status='failed',message=f'任务未完成（{type(exc).__name__}）。已保留已有结果，可查看配置与重新创建任务。')
    finally:stop.set();thread.join(timeout=1)
    return True

if __name__=='__main__':
    init_db();owner=uid('worker')
    print('Story worker ready',flush=True)
    while True:
        with Session.begin() as db:
            row=db.get(Record,'worker_heartbeat')
            if row:row.data={'at':time.time()}
            else:db.add(Record(id='worker_heartbeat',kind='system',data={'at':time.time()}))
        if not process_one(owner):time.sleep(1)
