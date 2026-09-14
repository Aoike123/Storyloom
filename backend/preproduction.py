"""Casting and sets are approved before shot design."""
import hashlib,json,re,time
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

def storyboard_asset_contract(prep):
    """Give the model copy-safe reference groups instead of making it infer ID relations."""
    assets=prep.get('assets',{})
    character_sets=[]
    for character_id,character in assets.items():
        if character.get('role')!='character':continue
        if _is_condensed_character_reference(character,assets):continue
        costumes=[(costume_id,costume) for costume_id,costume in assets.items()
                  if costume.get('role')=='costume' and costume.get('identity_asset_id')==character_id]
        choices=costumes if character.get('requires_costume') else [(None,None),*costumes]
        for costume_id,costume in choices:
            ids=[character_id,*([costume_id] if costume_id else [])]
            if len(ids)<3:
                character_sets.append({'character':character.get('name') or character_id,
                    'costume':costume.get('name') if costume else None,'asset_ids':ids})
    scenes=[{'name':spec.get('name') or asset_id,'asset_id':asset_id}
            for asset_id,spec in assets.items() if spec.get('role')=='scene']
    props=[{'name':spec.get('name') or asset_id,'asset_id':asset_id}
           for asset_id,spec in assets.items() if spec.get('role')=='prop']
    return {
        'max_semantic_assets_per_shot':12,
        'max_reference_files_after_packing':3,
        'scene_required':True,
        'scene_count_per_shot':1,
        'character_reference_sets':character_sets,
        'scene_references':scenes,
        'prop_references':props,
        'selection_rule':('assets 必须使用真实 ID。每个入镜角色完整复制一组 character_reference_sets.asset_ids，'
                          '再追加一张 scene_references.asset_id；不要为了三图上限省略身份或服装。'
                          '调用方仅在完整列表超过三张时，才会把同一角色的身份与服装确定性拼成一张参考板；'
                          '若拼接后仍超过三张，校验会要求拆成反打或空场景镜头。'),
    }


def storyboard_preproduction(prep):
    """Attach the normalized binding contract without mutating the saved setup."""
    assets=prep.get('assets',{})
    visible={asset_id:spec for asset_id,spec in assets.items() if not _is_condensed_character_reference(spec,assets)}
    return {**prep,'assets':visible,'asset_binding_contract':storyboard_asset_contract(prep)}


def shot_prompt_preproduction(prep):
    """Prompt compilation must see any physical stitched references selected by the caller."""
    return {**prep,'asset_binding_contract':storyboard_asset_contract(prep)}


def _is_condensed_character_reference(spec,assets):
    return (spec.get('role')=='character' and bool(spec.get('costume_asset_id'))
            and spec.get('identity_asset_id') in assets and spec.get('costume_asset_id') in assets)


def _asset_label(asset_id,assets):
    spec=assets.get(asset_id,{})
    return f'{spec.get("name") or asset_id}（{asset_id}）'


def _shot_character_mentions(shot,assets):
    """Resolve only unique approved-character names that visibly occur in the image prompt."""
    prompt=getattr(shot,'reference_prompt','') or ''
    base_characters={asset_id:spec for asset_id,spec in assets.items()
                     if spec.get('role')=='character' and not spec.get('costume_asset_id')}
    alias_owners={}
    for asset_id,spec in base_characters.items():
        name=(spec.get('name') or '').strip();aliases={name} if name else set()
        if len(name)>=4 and re.fullmatch(r'[\u3400-\u9fff]+',name):
            aliases.update(name[index:index+3] for index in range(len(name)-2))
        for alias in aliases:alias_owners.setdefault(alias,[]).append(asset_id)
    return [asset_id for asset_id in base_characters if any(
        alias in prompt and owners==[asset_id] for alias,owners in alias_owners.items())]


def _character_reference_coverage(ids,assets,character_id):
    identity=False;costume=False
    for asset_id in ids:
        spec=assets.get(asset_id,{})
        if spec.get('role')=='character' and (spec.get('identity_asset_id') or asset_id)==character_id:
            identity=True
            if spec.get('costume_asset_id'):costume=True
        if spec.get('role')=='costume' and spec.get('identity_asset_id')==character_id:costume=True
    return identity,costume


def plan_board_asset_packing(board,prep):
    """Plan the minimum identity+costume stitches needed per over-limit shot, without writing files."""
    assets=prep['assets'];result={}
    for shot in board.shots:
        ids=list(dict.fromkeys(shot.assets))
        needed=max(0,len(ids)-3)
        if not needed:continue
        pairs=[]
        for character_id in ids:
            spec=assets.get(character_id,{})
            if spec.get('role')!='character' or spec.get('costume_asset_id'):continue
            costumes=[costume_id for costume_id in ids if assets.get(costume_id,{}).get('role')=='costume'
                      and assets[costume_id].get('identity_asset_id')==character_id]
            if len(costumes)==1:pairs.append((character_id,costumes[0]))
        if pairs:result[shot.id]=pairs[:needed]
    return result


def validate_board(board,prep,packing=None):
    assets=prep['assets'];known=set(assets);issues=[]
    packing=packing or {}
    from .asset_workflow import validate_shot_identities
    for shot in board.shots:
        ids=list(shot.assets)
        if not ids:
            issues.append(f'{shot.id} 的 assets 为空，必须按 asset_binding_contract 绑定参考图。')
            continue
        packed_count=len(ids)-len(packing.get(shot.id,[]))
        if packed_count>3:
            labels='、'.join(_asset_label(asset_id,assets) for asset_id in ids)
            issues.append(f'{shot.id} 的 assets 完整绑定为 {len(ids)} 张（{labels}），人物服装条件拼接后仍需 {packed_count} 张，'
                          '当前模型每镜最多接收 3 张；多人内容必须拆成单人反打或空场景镜头。')
        duplicates=list(dict.fromkeys(asset_id for index,asset_id in enumerate(ids) if asset_id in ids[:index]))
        if duplicates:
            issues.append(f'{shot.id} 的 assets 重复绑定：'+ '、'.join(duplicates)+'。每个 ID 只能出现一次。')
        unknown=list(dict.fromkeys(asset_id for asset_id in ids if asset_id not in known))
        if unknown:
            issues.append(f'{shot.id} 的 assets 含未选定素材 ID：'+ '、'.join(unknown)+
                          '。只能复制 preproduction.asset_binding_contract 中列出的真实 ID。')
        selected=list(dict.fromkeys(asset_id for asset_id in ids if asset_id in known))
        scenes=[asset_id for asset_id in selected if assets[asset_id]['role']=='scene']
        if not scenes:
            issues.append(f'{shot.id} 未绑定已批准场景；必须选择 scene_references 中与 scene 对应的一张图。')
        elif len(scenes)>1:
            issues.append(f'{shot.id} 同时绑定了多个场景：'+ '、'.join(scenes)+'。每镜只能使用一个场景。')
        mentioned=_shot_character_mentions(shot,assets)
        represented=[]
        for asset_id in selected:
            spec=assets[asset_id]
            if spec.get('role')=='character':
                represented.append(spec.get('identity_asset_id') or asset_id)
        for character_id in dict.fromkeys([*mentioned,*represented]):
            spec=assets.get(character_id,{})
            has_identity,has_costume=_character_reference_coverage(selected,assets,character_id)
            if character_id in mentioned and not has_identity:
                issues.append(f'{shot.id} 的 reference_prompt 出现已确认角色“{spec.get("name") or character_id}”，'
                              f'但 assets 未绑定其身份图 {character_id}；请完整绑定，或从画面描述移除未入镜角色。')
            if has_identity and spec.get('requires_costume') and not has_costume:
                costumes=[asset_id for asset_id,item in assets.items()
                          if item.get('role')=='costume' and item.get('identity_asset_id')==character_id]
                choices='、'.join(costumes) or '当前没有可用服装图'
                issues.append(f'{shot.id} 已绑定角色“{spec.get("name") or character_id}”但缺少对应服装；'
                              f'请从该角色服装中选择一张：{choices}。')
        for asset_id in selected:
            spec=assets[asset_id]
            if not _is_condensed_character_reference(spec,assets):continue
            expanded=list(dict.fromkeys([*(item for item in selected if item!=asset_id),
                spec['identity_asset_id'],spec['costume_asset_id']]))
            if len(expanded)<=3:
                issues.append(f'{shot.id} 未达到三图上限却使用了人物服装拼接参考板 {asset_id}；'
                              '请直接绑定原人物身份图与服装图，只有超限时才允许拼接。')
        if not unknown:
            try:validate_shot_identities(selected,assets)
            except HTTPException as exc:issues.append(f'{shot.id} 人物与服装映射错误：{exc.detail}')
    if issues:raise HTTPException(422,'；'.join(issues))


def add_condensed_reference_alternatives(db,pid,pairs):
    """Materialize only the identity+costume pairs selected by an over-limit shot plan."""
    config=db.get(Record,'prep_'+pid)
    if not config:raise HTTPException(409,'前期素材配置不存在，不能制作条件拼接参考。')
    assets=dict(config.data['assets']);versions=dict(config.data['versions'])
    dependencies={item['asset_revision_id']:item for item in config.data.get('source_dependencies',[])}
    added=[]
    from .asset_workflow import stitch_character_costume_reference
    for identity_id,costume_id in dict.fromkeys(tuple(pair) for pair in pairs):
        identity=db.get(Record,identity_id);costume=db.get(Record,costume_id)
        identity_spec=assets.get(identity_id,{});costume_spec=assets.get(costume_id,{})
        if (identity_spec.get('role')!='character' or costume_spec.get('role')!='costume'
                or costume_spec.get('identity_asset_id')!=identity_id):
            raise HTTPException(422,'条件拼接的人物与服装映射无效。')
        combined=stitch_character_costume_reference(db,identity,costume,
            (identity_spec.get('name') or identity_id)+' · '+(costume_spec.get('name') or costume_id))
        assets[combined.id]={'role':'character','name':combined.data['name'],'notes':combined.data['description'],
            'identity_asset_id':identity_id,'costume_asset_id':costume_id,'requires_costume':False}
        versions[combined.id]=combined.version
        if combined.id not in config.data['assets']:added.append(combined.id)
        for dependency in combined.data['asset_dependencies']:
            dependencies[dependency['asset_revision_id']]=dependency
    if not added:return ready(db,pid),[]
    config.data={**config.data,'assets':assets,'versions':versions,'source_dependencies':list(dependencies.values())}
    config.version+=1
    _,token=snapshot(db,pid)
    gate=db.get(Record,'prep_gate_'+pid)
    data={'stamp':token,'assets':dict(versions),'mode':'reference_images',
          'note':'保留已确认原图；仅为实际超过三图上限的镜头添加本地人物服装拼接参考板。'}
    if gate and gate.data.get('history'):data['history']=gate.data['history']
    if gate:gate.data=data;gate.version+=1
    else:db.add(Record(id='prep_gate_'+pid,kind='preproduction_gate',data=data))
    db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'conditional_reference_stitching_added',
        'asset_ids':added,'model_call':False,'preserved_base_assets':True}))
    return ready(db,pid),added


def materialize_board_asset_packing(pid,task_id,stamp,packing):
    """Commit a validated per-shot packing plan and update the running board task to its new stamp."""
    pairs=[pair for shot_pairs in packing.values() for pair in shot_pairs]
    if not pairs:raise HTTPException(409,'没有需要拼接的超限镜头。')
    with Session.begin() as db:
        current=ready(db,pid)
        if current['stamp']!=stamp:raise HTTPException(409,'已确认素材发生变化，不能应用条件拼接。')
        current,added=add_condensed_reference_alternatives(db,pid,pairs)
        project_row=project(db,pid);project_row.data={**project_row.data,'preproduction_stamp':current['stamp']};project_row.version+=1
        task=db.get(Task,task_id)
        if task and task.payload.get('project_id')==pid:task.payload={**task.payload,'preproduction':current}
        run=db.get(Record,'creative_'+pid)
        if run:run.data={**run.data,'reference_inputs':'base_sheets_with_conditional_stitching'};run.version+=1
        return current,added


def repair_board_assets(board,prep):
    """Repair only unambiguous reference-list mistakes; never infer creative content."""
    repaired=board.model_copy(deep=True);changes=[]
    assets=prep['assets']
    condensed={(spec.get('identity_asset_id'),spec.get('costume_asset_id')):asset_id
               for asset_id,spec in assets.items() if _is_condensed_character_reference(spec,assets)}
    for shot in repaired.shots:
        unique=list(dict.fromkeys(shot.assets))
        if unique!=shot.assets:
            removed=[aid for index,aid in enumerate(shot.assets) if aid in shot.assets[:index]]
            changes.append({'shot_id':shot.id,'action':'remove_duplicate_assets','asset_ids':removed})
        for character_id in _shot_character_mentions(shot,assets):
            has_identity,_=_character_reference_coverage(unique,assets,character_id)
            if not has_identity:
                unique.append(character_id)
                changes.append({'shot_id':shot.id,'action':'bind_named_character','asset_id':character_id})
        represented=[]
        for asset_id in unique:
            spec=assets.get(asset_id,{})
            if spec.get('role')=='character':represented.append(spec.get('identity_asset_id') or asset_id)
        for character_id in dict.fromkeys(represented):
            spec=assets.get(character_id,{})
            _,has_costume=_character_reference_coverage(unique,assets,character_id)
            if not spec.get('requires_costume') or has_costume:continue
            costumes=[asset_id for asset_id,item in assets.items()
                      if item.get('role')=='costume' and item.get('identity_asset_id')==character_id]
            if len(costumes)==1:
                unique.append(costumes[0])
                changes.append({'shot_id':shot.id,'action':'bind_unique_character_costume',
                                'asset_id':costumes[0],'identity_asset_id':character_id})
        has_scene=any(aid in prep['assets'] and prep['assets'][aid]['role']=='scene' for aid in unique)
        if not has_scene and len(unique)<3:
            location=shot.scene.strip();matches=[]
            for aid,spec in prep['assets'].items():
                name=spec.get('name','').strip()
                suffix=location[len(name):len(name)+1] if name and location.startswith(name) else ''
                if spec.get('role')=='scene' and name and (location==name or suffix and suffix in '，,、；;：:（( '):matches.append(aid)
            if len(matches)==1:
                unique.append(matches[0]);changes.append({'shot_id':shot.id,'action':'bind_unique_named_scene','asset_id':matches[0]})
        if len(unique)>3:
            pairs=[]
            for character_id in unique:
                spec=assets.get(character_id,{})
                if spec.get('role')!='character' or spec.get('costume_asset_id'):continue
                costumes=[costume_id for costume_id in unique if assets.get(costume_id,{}).get('role')=='costume'
                          and assets[costume_id].get('identity_asset_id')==character_id]
                if len(costumes)==1 and (character_id,costumes[0]) in condensed:
                    pairs.append((min(unique.index(character_id),unique.index(costumes[0])),character_id,costumes[0],condensed[(character_id,costumes[0])]))
            for _,character_id,costume_id,reference_id in sorted(pairs):
                if len(unique)<=3:break
                position=min(unique.index(character_id),unique.index(costume_id))
                unique=[asset_id for asset_id in unique if asset_id not in (character_id,costume_id)]
                unique.insert(position,reference_id)
                changes.append({'shot_id':shot.id,'action':'pack_identity_costume_reference','asset_id':reference_id,
                    'source_asset_ids':[character_id,costume_id]})
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
                spec.identity_asset_id=aid
            if a.data.get('asset_kind')=='costume_sheet':
                identity=db.get(Record,spec.identity_asset_id or '')
                identity_spec=body.assets.get(spec.identity_asset_id or '')
                if spec.role!='costume' or not identity or not identity_spec or identity_spec.role!='character' or a.data.get('asset_spec',{}).get('character_ref')!=identity.data.get('asset_spec',{}).get('character_id'):
                    raise HTTPException(422,'独立服装必须绑定本次选定的正确人物身份图。')
            if a.data.get('asset_kind') in ('dressed_character','character_costume_reference'):
                spec=body.assets[aid]
                if spec.role!='character' or spec.identity_asset_id!=a.data.get('identity_asset_id') or spec.costume_asset_id!=a.data.get('costume_asset_id'):
                    raise HTTPException(422,'人物服装组合参考必须保留真实的身份和服装依赖，不能重新绑定。')
                from .asset_workflow import validate_dependencies
                validate_dependencies(db,a.data)
                for dependency in a.data['asset_dependencies']:dependencies[dependency['asset_revision_id']]=dependency
        for aid,spec in body.assets.items():
            asset=db.get(Record,aid)
            if spec.role!='character' or not asset or asset.data.get('asset_kind')!='character_sheet':continue
            has_costume=any(item.role=='costume' and item.identity_asset_id==aid for item in body.assets.values())
            costume_mode=asset.data.get('asset_spec',{}).get('costume_mode','required')
            if costume_mode=='required' and not has_costume:
                raise HTTPException(422,'人物身份参考缺少对应的独立服装图。')
            spec.requires_costume=has_costume
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
