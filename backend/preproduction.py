"""Casting and sets are approved before shot design."""
import hashlib,json,time
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from typing import Literal
from sqlalchemy import select
from .db import Session,Record,Task,uid,record_dict,task_dict
from .image_provider import local_frame_data
from .providers import settings
from .reference_image_model import REFERENCE_IMAGE_MODEL, REFERENCE_IMAGE_STEPS
from .skill_runtime import render_node
router=APIRouter(prefix='/api/preproduction',tags=['preproduction'])

def project(db,pid):
    p=db.get(Record,pid)
    if not p or p.kind!='director':raise HTTPException(404,'方案不存在。')
    if p.data.get('archived'):raise HTTPException(409,'请先恢复方案。')
    return p


def current_base_asset_versions(db,pid):
    """Return the exact current base task -> asset revision bindings for a creative run."""
    run=db.get(Record,'creative_'+pid)
    if not run:return {}
    bindings={}
    for item in run.data.get('items',[]):
        task_id=item.get('task_id')
        task=db.get(Task,task_id or '')
        asset_id=task.result.get('asset_id') if task else None
        asset=db.get(Record,asset_id or '')
        if not task_id or not task or task.status!='completed' or not asset or asset.kind!='asset':
            raise HTTPException(409,'基础素材尚未全部完成，请处理失败任务后重新绑定最新版。')
        bindings[task_id]={'asset_id':asset.id,'asset_version':asset.version}
    return bindings


def _invalidate_record(row,reason,replacement_task_id,at):
    if not row:return False
    snapshot={k:v for k,v in row.data.items() if k!='history'}
    history=[*row.data.get('history',[]),{'version':row.version,'data':snapshot,
        'invalidated_at':at,'reason':reason,'replacement_task_id':replacement_task_id}]
    row.data={**row.data,'history':history,'invalidated_at':at,'invalidated_reason':reason,
        'replacement_task_id':replacement_task_id}
    if 'approved' in row.data:row.data={**row.data,'approved':False}
    row.version+=1
    return True


def invalidate_downstream_references(db,pid,reason,replacement_task_id):
    """Invalidate every current pointer derived from replaced base artwork without deleting history."""
    at=time.time();invalidated=[]
    for rid in ('prep_'+pid,'prep_gate_'+pid,'visual_'+pid,'visual_gate_'+pid):
        if _invalidate_record(db.get(Record,rid),reason,replacement_task_id,at):invalidated.append(rid)
    current=project(db,pid)
    board_keys=('board','review','board_diagnostics','preproduction_stamp','board_recovery')
    if any(key in current.data for key in board_keys):
        saved={key:current.data.get(key) for key in board_keys if key in current.data}
        history=[*current.data.get('board_history',[]),{**saved,'version':current.version,
            'invalidated_at':at,'reason':reason,'replacement_task_id':replacement_task_id}]
        data={key:value for key,value in current.data.items() if key not in board_keys}
        current.data={**data,'board_history':history,'requires_preproduction':True,
            'status':'awaiting_preproduction','downstream_invalidated_at':at,
            'downstream_invalidation_reason':reason,'replacement_task_id':replacement_task_id}
        current.version+=1;invalidated.append(current.id)
    db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'downstream_references_invalidated',
        'reason':reason,'replacement_task_id':replacement_task_id,'records':invalidated,'at':at}))
    return invalidated

def snapshot(db,pid):
    r=db.get(Record,'prep_'+pid)
    if not r:raise HTTPException(409,'请先完成选角和搭影棚。')
    if r.data.get('invalidated_at'):raise HTTPException(409,'基础素材已重做，请重新绑定最新版。')
    expected=r.data.get('source_assets')
    if expected is not None and expected!=current_base_asset_versions(db,pid):
        raise HTTPException(409,'基础素材已重做，请重新绑定最新版。')
    for aid,version in r.data['versions'].items():
        a=db.get(Record,aid)
        if not a or a.version!=version or a.data.get('status')!='approved':raise HTTPException(409,'参考图已变化，请重新保存并确认当前设定。')
    from .asset_workflow import validate_dependencies
    validate_dependencies(db,{'asset_dependencies':r.data.get('source_dependencies',[])})
    token=hashlib.sha256(json.dumps(r.data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return r,token

def ready(db,pid):
    project(db,pid);r,token=snapshot(db,pid)
    gate=db.get(Record,'prep_gate_'+pid)
    if not gate or gate.data.get('stamp')!=token:raise HTTPException(409,'请确认人物、服装与场景参考图后再生成正式分镜。')
    for aid,v in gate.data['assets'].items():
        a=db.get(Record,aid)
        if not a or a.version!=v or a.data.get('status')!='approved':raise HTTPException(409,'参考图确认已失效。')
    return {**r.data,'stamp':token}

def validate_board(board,prep):
    known=set(prep['assets'])
    for s in board.shots:
        if not s.assets or len(s.assets)>3 or len(set(s.assets))!=len(s.assets) or not set(s.assets)<=known:raise HTTPException(422,f'{s.id} 引用了未选定的演员、影棚或道具，需调整分镜或返回搭景。')
        if not any(prep['assets'][a]['role']=='scene' for a in s.assets):raise HTTPException(422,f'{s.id} 未绑定已批准影棚。')
        from .asset_workflow import validate_shot_identities
        validate_shot_identities(s.assets,prep['assets'])


def repair_board_assets(board,prep):
    """Repair only unambiguous reference-list mistakes; never infer creative content."""
    repaired=board.model_copy(deep=True);changes=[]
    for shot in repaired.shots:
        unique=list(dict.fromkeys(shot.assets))
        if unique!=shot.assets:
            removed=[aid for index,aid in enumerate(shot.assets) if aid in shot.assets[:index]]
            changes.append({'shot_id':shot.id,'action':'remove_duplicate_assets','asset_ids':removed})
        has_scene=any(aid in prep['assets'] and prep['assets'][aid]['role']=='scene' for aid in unique)
        if not has_scene and len(unique)<3:
            location=shot.scene.strip();matches=[]
            for aid,spec in prep['assets'].items():
                name=spec.get('name','').strip()
                suffix=location[len(name):len(name)+1] if name and location.startswith(name) else ''
                if spec.get('role')=='scene' and name and (location==name or suffix and suffix in '，,、；;：:（( '):matches.append(aid)
            if len(matches)==1:
                unique.append(matches[0]);changes.append({'shot_id':shot.id,'action':'bind_unique_named_scene','asset_id':matches[0]})
        shot.assets=unique
    return repaired,changes

class AssetSpec(BaseModel):
    role:Literal['character','costume','scene','prop']
    name:str=Field(min_length=1,max_length=100)
    notes:str=Field(min_length=5,max_length=1500)
    identity_asset_id:str|None=None
    costume_asset_id:str|None=None
    requires_costume:bool=False
class Setup(BaseModel):
    expected_version:int=0
    style:str=Field(min_length=10,max_length=2000)
    assets:dict[str,AssetSpec]
    source_assets:dict[str,dict[str,int|str]]|None=None

@router.get('/{pid}')
def get(pid:str):
    with Session() as db:
        p=project(db,pid);r=db.get(Record,'prep_'+pid)
        token=None;passed=False;reason='先选角并搭建影棚'
        try:
            _,token=snapshot(db,pid);ready(db,pid);passed=True;reason='参考图已确认，可编写组合分镜'
        except HTTPException as e:reason=e.detail
        jobs=[t for t in db.scalars(select(Task).where(Task.kind=='image').order_by(Task.created.desc())) if t.payload.get('preproduction_id')==pid and t.payload.get('preproduction_stamp')==token]
        previous=db.get(Record,'prep_'+p.data.get('restart_of','')) if p.data.get('restart_of') else None
        return {'previous_config':previous.data if previous else None,'config':record_dict(r) if r else None,'stamp':token,'approved':passed,'reason':reason,
            'assets':[record_dict(a) for a in db.scalars(select(Record).where(Record.kind=='asset')) if a.data.get('media') and a.data.get('status')=='approved'],
            'trials':[{'task':task_dict(t),'asset':record_dict(a) if (a:=db.get(Record,t.result.get('asset_id',''))) else None} for t in jobs]}

@router.post('/{pid}')
def save(pid:str,body:Setup):
    with Session.begin() as db:
        p=project(db,pid)
        if any(t.payload.get('project_id')==pid or t.payload.get('preproduction_id')==pid or t.payload.get('director_id')==pid or t.payload.get('creative_id')==pid for t in db.scalars(select(Task).where(Task.status.in_(['queued','running','waiting'])))):raise HTTPException(409,'请等待当前制作任务结束再修改选角与搭景。')
        roles={a.role for a in body.assets.values()}
        if not {'character','scene'}<=roles:raise HTTPException(422,'至少选择一位角色和一个影棚。')
        versions={};dependencies={}
        for aid in body.assets:
            a=db.get(Record,aid)
            if not a or a.kind!='asset' or a.data.get('status')!='approved':raise HTTPException(422,'请使用资产库已审核的参考图。')
            from .asset_workflow import validate_asset_origin
            validate_asset_origin(db,a)
            try:local_frame_data(a.data.get('media'))
            except Exception:raise HTTPException(422,'参考图文件无效。') from None
            versions[aid]=a.version
            spec=body.assets[aid]
            if a.data.get('asset_kind')=='character_sheet':
                if spec.role!='character':raise HTTPException(422,'人物身份图必须作为人物参考。')
                spec.identity_asset_id=aid;spec.requires_costume=True
            if a.data.get('asset_kind')=='costume_sheet':
                identity=db.get(Record,spec.identity_asset_id or '')
                identity_spec=body.assets.get(spec.identity_asset_id or '')
                if spec.role!='costume' or not identity or not identity_spec or identity_spec.role!='character' or a.data.get('asset_spec',{}).get('character_ref')!=identity.data.get('asset_spec',{}).get('character_id'):
                    raise HTTPException(422,'独立服装必须绑定本次选定的正确人物身份图。')
            if a.data.get('asset_kind')=='dressed_character':
                spec=body.assets[aid]
                if spec.role!='character' or spec.identity_asset_id!=a.data.get('identity_asset_id') or spec.costume_asset_id!=a.data.get('costume_asset_id'):
                    raise HTTPException(422,'定装结果必须保留真实的人物身份和服装依赖，不能重新绑定。')
                from .asset_workflow import validate_dependencies
                validate_dependencies(db,a.data)
                for dependency in a.data['asset_dependencies']:dependencies[dependency['asset_revision_id']]=dependency
        for aid,spec in body.assets.items():
            if spec.role=='character' and spec.requires_costume and not any(item.role=='costume' and item.identity_asset_id==aid for item in body.assets.values()):
                raise HTTPException(422,'人物身份参考缺少对应的独立服装图。')
        r=db.get(Record,'prep_'+pid)
        if (r.version if r else 0)!=body.expected_version:raise HTTPException(409,'设定已更新，请刷新。')
        source_assets=body.model_dump().get('source_assets')
        if source_assets is not None and source_assets!=current_base_asset_versions(db,pid):
            raise HTTPException(409,'基础素材已重做，请重新绑定最新版。')
        data={**body.model_dump(exclude={'expected_version'}),'versions':versions,'source_dependencies':list(dependencies.values())}
        if r and r.data.get('history'):data['history']=r.data['history']
        if r:r.data=data;r.version+=1
        else:r=Record(id='prep_'+pid,kind='preproduction',data=data);db.add(r)
        db.flush();return record_dict(r)

class Trial(BaseModel):
    stamp:str
    assets:list[str]=Field(min_length=2,max_length=3)
    prompt:str=Field(min_length=5,max_length=1500)
    confirm_paid:bool=False

@router.post('/{pid}/trial')
def trial(pid:str,body:Trial):
    with Session() as db:
        run=db.get(Record,'creative_'+pid)
        if run and run.data.get('direct_reference_inputs'):raise HTTPException(409,'当前流程不再创建试拍，直接使用基础参考图生成分镜。')
    if not body.confirm_paid:raise HTTPException(422,'请确认试拍生图费用。')
    cfg=settings()
    if not cfg.get('image_paid_enabled',cfg['paid_enabled']) or not cfg['image_configured']:raise HTTPException(422,'请配置生图接口。')
    with Session.begin() as db:
        project(db,pid);r,token=snapshot(db,pid)
        if token!=body.stamp:raise HTTPException(409,'设定已变更。')
        if len(set(body.assets))!=len(body.assets) or not set(body.assets)<=set(r.data['assets']):raise HTTPException(422,'参考图必须来自本次选角与影棚。')
        if not {'character','scene'}<={r.data['assets'][a]['role'] for a in body.assets}:raise HTTPException(422,'试拍必须同时包含演员与影棚。')
        refs=[db.get(Record,a) for a in body.assets]
        t=Task(id=uid('image'),kind='image',payload={'mode':'live','preproduction_id':pid,'preproduction_stamp':token,'reference_ids':body.assets,
            'reference_media':[a.data['media'] for a in refs],'image_model':REFERENCE_IMAGE_MODEL,
            'image_inference_steps':REFERENCE_IMAGE_STEPS,'title':'定装与影棚试拍',
            **render_node('scene_trial',{'style':r.data['style'],'references':json.dumps([r.data['assets'][a] for a in body.assets],ensure_ascii=False),'requirements':body.prompt})})
        db.add(t);db.flush();return task_dict(t)

class Approve(BaseModel):
    stamp:str
    asset_ids:list[str]=Field(min_length=1,max_length=30)
    note:str=Field(min_length=10,max_length=1000)
    confirm:bool=False


def approve_references(pid,stamp):
    """Lock already confirmed character/scene references without generating trial images."""
    with Session.begin() as db:
        project(db,pid);references,token=snapshot(db,pid)
        if stamp!=token:raise HTTPException(409,'参考图已变化，请重新确认。')
        if not {'character','scene'}<={asset['role'] for asset in references.data['assets'].values()}:
            raise HTTPException(422,'组合分镜至少需要人物身份图和场景图。')
        gate=db.get(Record,'prep_gate_'+pid)
        data={'stamp':token,'assets':dict(references.data['versions']),'mode':'reference_images',
              'note':'直接使用已确认的人物身份、独立服装与场景参考图编写组合分镜；没有定装或试拍合成。'}
        if gate and gate.data.get('history'):data['history']=gate.data['history']
        if gate:gate.data=data;gate.version+=1
        else:db.add(Record(id='prep_gate_'+pid,kind='preproduction_gate',data=data))
        db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'reference_inputs_locked','asset_ids':list(data['assets']),'stamp':token}))
    return {'approved':True,'mode':'reference_images'}

@router.post('/{pid}/approve')
def approve(pid:str,body:Approve):
    if not body.confirm:raise HTTPException(422,'请确认角色、服装、比例、空间和画风。')
    with Session.begin() as db:
        project(db,pid);r,token=snapshot(db,pid)
        if token!=body.stamp:raise HTTPException(409,'设定已变更。')
        versions={};covered=set()
        for aid in body.asset_ids:
            a=db.get(Record,aid);t=db.get(Task,a.data.get('source_task','')) if a else None
            if not a or a.data.get('status')!='approved' or not t or t.payload.get('preproduction_id')!=pid or t.payload.get('preproduction_stamp')!=token:raise HTTPException(422,'试拍必须来自当前设定，并先审核图片。')
            versions[aid]=a.version;covered.update(t.payload['reference_ids'])
        if not set(r.data['assets'])<=covered:raise HTTPException(422,'试拍需要覆盖所有选定演员、影棚及道具；可分组生成。')
        gate=db.get(Record,'prep_gate_'+pid);data={'stamp':token,'assets':versions,'note':body.note}
        if gate and gate.data.get('history'):data['history']=gate.data['history']
        if gate:gate.data=data;gate.version+=1
        else:db.add(Record(id='prep_gate_'+pid,kind='preproduction_gate',data=data))
        return {'approved':True}
