"""Result-oriented art direction; professional settings stay behind the review UI."""
import time,json,re,hashlib
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field,ValidationError
from sqlalchemy import select
from .db import Session,Record,Task,uid,record_dict,task_dict
from .providers import chat_json,payment_message,settings,ProviderError,ModelOutputError
from . import preproduction as prep, director as director, consistency as visual, production
from . import asset_sheets
from .skill_runtime import call_node
router=APIRouter(prefix='/api/creative',tags=['creative'])
BUSY=('queued','running','waiting')

def get_project(db,pid):return prep.project(db,pid)


def segment_roster(cut):
    """Who and where the cut film actually needs, so the design stage draws nothing else.

    The cut already names the characters and location of every scene. Handing that list to the
    design nodes is what keeps a story's background cast and unused places from being drawn and
    billed: only what appears in an ordered scene gets an identity, a costume or a set.
    """
    if not isinstance(cut,dict):return []
    roster=[]
    for segment in cut.get('segments') or []:
        roster.append({'segment_id':segment.get('id'),'title':segment.get('title'),
            'characters':list(segment.get('characters') or []),'location':segment.get('location') or '',
            'shot_budget':segment.get('shot_budget')})
    return roster


def get_run(db,pid):
    r=db.get(Record,'creative_'+pid)
    if not r:raise HTTPException(409,'先选择画风和剧情风格。')
    return r

def save_run(pid,**data):
    with Session.begin() as db:
        r=get_run(db,pid);r.data={**r.data,**data};r.version+=1

def no_active(db,pid):
    if any(t.payload.get('creative_id')==pid or t.payload.get('project_id')==pid or t.payload.get('director_id')==pid or t.payload.get('preproduction_id')==pid for t in db.scalars(select(Task).where(Task.status.in_(BUSY)))):raise HTTPException(409,'制作仍在进行，请等待完成后修改。')

class Style(BaseModel):
    art:str=Field(min_length=2,max_length=500)
    tone:str=Field(min_length=2,max_length=300)
    confirm_paid:bool=False

def paid(body,*kinds):
    """Gate a step on the providers it actually needs.

    Gating on all three providers meant a spent video budget blocked text-only steps with a
    message about confirming fees, even though the visitor had confirmed them.
    """
    if not body.confirm_paid:raise HTTPException(422,'请确认本次制作调用模型产生的费用。')
    message=payment_message(*(kinds or ('llm','image','video')),cfg=settings())
    if message:raise HTTPException(422,message)

@router.post('/{pid}/design')
def design(pid:str,body:Style):
    paid(body,'llm','image')
    with Session.begin() as db:
        p=get_project(db,pid);no_active(db,pid)
        if not p.data.get('treatment'):raise HTTPException(409,'请先完成导演阐述。')
        original=db.get(Task,p.data['task_id'])
        old=db.get(Record,'creative_'+pid)
        if old:raise HTTPException(409,'已有设计，请在图片上提出修改意见；整体换风格请归档后重做。')
        t=Task(id=uid('art'),kind='art_design',payload={'mode':'live','creative_id':pid,'source':original.payload['source'],'treatment':p.data['treatment'],
            # The cut decides which characters and places are worth drawing, so it travels with the
            # design task instead of being produced later by the storyboard node.
            **({'segments':p.data['segments']} if p.data.get('segments') else {}),
            'art':body.art,'tone':body.tone,'asset_schema':asset_sheets.VERSION,'skill_pipeline':'node-skills-v1'})
        db.add(Record(id='creative_'+pid,kind='creative_run',data={'art':body.art,'tone':body.tone,'stage':'designing','items':[],'watch':t.id}))
        db.add(t);db.flush();return task_dict(t)


def redesign(db,pid,body):
    """Replace the initial asset plan while preserving previous images and prompts."""
    paid(body,'llm','image');p=get_project(db,pid);no_active(db,pid);run=get_run(db,pid)
    if run.data['stage']!='assets_review':raise HTTPException(409,'仅可在初次人物与场景确认阶段重做设定图。')
    original=db.get(Task,p.data['task_id'])
    task=Task(id=uid('art'),kind='art_design',payload={'mode':'live','creative_id':pid,
        'source':original.payload['source'],'treatment':p.data['treatment'],
        **({'segments':p.data['segments']} if p.data.get('segments') else {}),
        'art':body.art,'tone':body.tone,
        'asset_schema':asset_sheets.VERSION,'skill_pipeline':'node-skills-v1','replaces_assets':[i['task_id'] for i in run.data['items']]})
    prep.invalidate_downstream_references(db,pid,'基础人物、服装与场景已整体重做',task.id)
    history=[*run.data.get('design_history',[]),{k:v for k,v in run.data.items() if k!='design_history'}]
    run.data={'art':body.art,'tone':body.tone,'stage':'designing','items':[],'watch':task.id,'design_history':history}
    run.version+=1;db.add(task)

def source_refs(reference,passages):
    """Expand explicit citations only; never guess or discard an unknown reference."""
    refs=[]
    for part in re.split(r'[,，、;；]',reference):
        match=re.fullmatch(r'\s*(P[0-9]{3,})(?:\s*[-–—~～至]\s*(P[0-9]{3,}))?\s*',part)
        if not match:raise ProviderError('原文依据格式无效，请使用 P001、P003 或 P001-P003。')
        first,last=match.group(1),match.group(2) or match.group(1)
        for ref in (first,last):
            if ref not in passages:raise ProviderError(f'原文依据编号 {ref} 不存在。')
        start,end=int(first[1:]),int(last[1:])
        if start>end or end-start>=len(passages):raise ProviderError('原文依据范围无效。')
        for number in range(start,end+1):
            ref=f'P{number:03}'
            if ref not in passages:raise ProviderError(f'原文依据编号 {ref} 不存在。')
            if ref not in refs:refs.append(ref)
    return refs


def validate_design(raw,passages,version=None):
    if version!=asset_sheets.VERSION:raise ProviderError('旧版叙述式设计不能继续生图，请在素材区按身份、服装分离的新流程重做。')
    plan=validate_spec(asset_sheets.AssetSheetPlan,raw)
    bindings=[]
    for item in plan.items:
        try:bindings.append(source_refs(item.source_ref,passages))
        except ProviderError as exc:raise ModelOutputError(f'「{item.name}」{exc}设计已保留，未继续生图。') from None
    return plan,bindings


def validate_spec(schema,raw):
    try:return schema.model_validate(raw)
    except ValidationError as exc:
        details='；'.join('.'.join(str(x) for x in e['loc'])+'：'+e['msg'] for e in exc.errors(include_input=False,include_url=False)[:4])
        raise ModelOutputError('静态视觉规格校验未通过，结果已保存，未继续生图。'+details) from None


def validate_sourced_spec(schema,raw,passages,collection):
    result=validate_spec(schema,raw)
    try:
        for item in getattr(result,collection):source_refs(item.source_ref,passages)
    except ProviderError as exc:
        raise ModelOutputError('素材规格没有绑定有效原文依据：'+str(exc)) from None
    return result


def validate_costume_plan(raw,passages,characters):
    """Costume coverage is checked inside the node retry loop, so the model can fix it itself.

    A character that quietly disappears from the costume list is what made the stage look skipped,
    so the missing C ids are returned as retry feedback instead of failing much later.
    """
    result=validate_sourced_spec(asset_sheets.CostumePlan,raw,passages,'costumes')
    covered={costume.character_ref for costume in result.costumes}
    missing=[person.character_id for person in characters if person.character_id not in covered]
    if missing:
        raise ModelOutputError('角色 → 服装 → 环境必须对每个角色都给出服装结论，缺少这些角色的服装记录：'
            +'、'.join(missing)+
            '。着衣物的角色给出实际服装（mode=garment）；天然体表、不着衣物的角色使用空衣服模式（mode=bare），'
            'wardrobe 留空并用 bare_surface 写明自然体表依据；但颈部以下为人类身体分区的角色（例如兽首人身）'
            '必须给出实际服装，空衣服模式在这些角色身上等于要求一张裸露的人类身体画面，会被供应商拒绝。'
            '不要用空列表跳过整段。')
    return result


def cached_node_output(db,task_id,node,schema):
    task=db.get(Task,task_id)
    if not task:return None
    for error in reversed((task.result or {}).get('model_output_errors',[])):
        if error.get('node')!=node or not error.get('output_record'):continue
        record=db.get(Record,error['output_record'])
        data=record.data if record and record.kind=='node_output' else {}
        if data.get('task_id')!=task_id or data.get('node')!=node:continue
        try:return schema.model_validate(data.get('response')).model_dump()
        except ValidationError:continue
    return None


def saved_design(db,run,task_id):
    # Legacy drafts were stored on the run; its watch identifies their only design task.
    if run.data.get('raw_design_task_id',run.data.get('watch'))==task_id:
        raw=run.data.get('raw_design')
        if raw is not None:return raw
        identity=run.data.get('identity_plan');materials=run.data.get('wardrobe_scene_plan')
        if identity is not None:
            identity=validate_spec(asset_sheets.IdentityPlan,identity)
            if materials is None:
                costumes=run.data.get('costume_plan');scenes=run.data.get('scene_plan')
                if costumes is None:costumes=cached_node_output(db,task_id,'costume_spec',asset_sheets.CostumePlan)
                if scenes is None:scenes=cached_node_output(db,task_id,'scene_spec',asset_sheets.ScenePlan)
                if costumes is not None and scenes is not None:
                    costumes=validate_spec(asset_sheets.CostumePlan,costumes)
                    scenes=validate_spec(asset_sheets.ScenePlan,scenes)
                    materials={'items':[i.model_dump() for i in costumes.costumes]+[i.model_dump() for i in scenes.scenes]}
            if materials is None:return None
            materials=validate_spec(asset_sheets.WardrobeScenePlan,materials)
            return {'visual_style':identity.visual_style.model_dump(),
                    'items':[p.model_dump() for p in identity.characters]+[i.model_dump() for i in materials.items]}
    return None


def resume_saved_design(db,pid):
    run=db.get(Record,'creative_'+pid)
    if not run or run.data.get('stage')!='designing':return
    task=db.get(Task,run.data.get('watch',''))
    if not task or task.kind!='art_design' or task.status not in ('failed','needs_review'):return
    if task.payload.get('creative_id')!=pid:raise HTTPException(409,'美术设计任务与当前作品不一致，不能恢复。')
    try:
        raw=saved_design(db,run,task.id)
        if raw is None:raise HTTPException(409,'没有完整的已保存设计结果，请先核实原模型调用，不能自动重复提交。')
        validate_design(raw,director.source_passages(task.payload['source']['content']),task.payload.get('asset_schema'))
    except ProviderError as exc:raise HTTPException(409,str(exc)) from None
    run.data={**run.data,'raw_design':raw,'raw_design_task_id':task.id}
    task.status='queued';task.lease=0;task.owner=''
    task.message='已恢复保存的美术设计，继续核对原文并准备图片'


def resume_saved_storyboard(db,pid):
    run=db.get(Record,'creative_'+pid)
    if not run or run.data.get('stage')!='storyboarding':return
    watch=db.get(Task,run.data.get('watch',''))
    child=db.get(Task,watch.payload.get('child','')) if watch and watch.kind=='creative_watch' else None
    project=db.get(Record,pid)
    if not watch or watch.status not in ('failed','needs_review') or not child or child.kind!='director' or child.status not in ('failed','needs_review'):
        return
    if child.payload.get('project_id')!=pid or not project or project.data.get('task_id')!=child.id:
        raise HTTPException(409,'分镜任务与当前作品不一致，不能恢复。')
    raw=(project.data.get('board_diagnostics') or {}).get('raw')
    if raw is None:raise HTTPException(409,'没有完整的已保存分镜结果，不能自动重复提交模型。')
    try:
        current=prep.ready(db,pid)
        if current['stamp']!=child.payload.get('preproduction',{}).get('stamp'):
            raise HTTPException(409,'已确认素材发生变化，保存的分镜不能继续使用。')
        board=director.Board.model_validate(raw)
        repaired,changes=director.repair_board_causality(board)
        content=child.payload.get('source',{}).get('content','')
        if not content:raise HTTPException(409,'已保存的分镜任务缺少对应原文，不能安全复用，请重新运行当前节点。')
        # The saved board carries quotes that were already replaced by real passages, so the
        # strict invention check applies only to freshly generated boards.
        director.bind_sources(repaired.shots,director.source_passages(content),'source_quote')
        structural_issues=director.check_board(repaired,content)
        if structural_issues:
            raise HTTPException(409,'已保存分镜仍有结构问题，不能直接复用：'+'；'.join(structural_issues[:5])+'。请使用“重新运行当前节点”，新模型会收到这些原因。')
        repaired,asset_changes=prep.repair_board_assets(repaired,current);changes.extend(asset_changes)
        packing=prep.plan_board_asset_packing(repaired,current)
        prep.validate_board(repaired,current,packing)
        pairs=[pair for shot_pairs in packing.values() for pair in shot_pairs]
        if pairs:
            current,added=prep.add_condensed_reference_alternatives(db,pid,pairs)
            repaired,packed_changes=prep.repair_board_assets(repaired,current)
            changes.extend(packed_changes);prep.validate_board(repaired,current)
        else:added=[]
    except ValidationError as exc:
        raise HTTPException(409,'已保存分镜的格式仍不完整，不能自动重复提交模型。') from None
    except ProviderError as exc:
        raise HTTPException(409,str(exc)) from None
    except HTTPException as exc:
        raise HTTPException(409,str(exc.detail)) from None
    child.payload={**child.payload,'preproduction':current,'saved_board':repaired.model_dump()}
    child.status='queued';child.lease=0;child.owner='';child.message='已恢复保存的分镜，正在校正结构与素材绑定并继续专业提示词节点'
    watch.status='queued';watch.lease=0;watch.owner='';watch.message='已恢复保存的分镜，等待文本预审'
    project.data={**project.data,'preproduction_stamp':current['stamp'],
        'board_recovery':{'task_id':child.id,'changes':changes,'conditional_stitching_added':bool(added)}}
    db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'saved_storyboard_resumed','task_id':child.id,'changes':changes}))


def run_design(task_id,payload):
    passages=director.source_passages(payload['source']['content'])
    with Session() as db:
        r=get_run(db,payload['creative_id'])
        if r.data.get('watch')!=task_id:raise ProviderError('美术设计任务已更新，旧结果不再使用。')
        if r.data.get('items'):return
        raw=saved_design(db,r,task_id)
        checkpoints=dict(r.data)
    if raw is None:
        if payload.get('asset_schema')!=asset_sheets.VERSION:
            raise ProviderError('旧版设计任务不能继续生成叙述式素材，请按新流程重做。')
        def checkpoint(key,value):
            with Session.begin() as db:
                r=get_run(db,payload['creative_id'])
                if r.data.get('watch')!=task_id:raise ProviderError('美术设计任务已更新，旧结果不再使用。')
                r.data={**r.data,key:value,'raw_design_task_id':task_id}
        context={'source_passages':passages,'excerpt_scope':payload['treatment'].get('excerpt_scope',''),
                 'segment_roster':segment_roster(payload.get('segments'))}
        identity=checkpoints.get('identity_plan')
        if identity is None:
            style=checkpoints.get('style_plan')
            if style is None:
                def style_contract(raw):
                    result=validate_spec(asset_sheets.StylePlan,raw)
                    asset_sheets.validate_style_intent(result.visual_style,payload['art'])
                    return result
                style,_=call_node(chat_json,'style_spec',{'art_preference':payload['art'],'schema':asset_sheets.StylePlan.model_json_schema()},task_id,
                    validator=style_contract)
                checkpoint('style_plan',style.model_dump())
            else:style=validate_spec(asset_sheets.StylePlan,style)
            people=checkpoints.get('character_plan')
            if people is None:
                people,_=call_node(chat_json,'identity_spec',{**context,'visual_style':style.visual_style.model_dump(),'schema':asset_sheets.CharacterPlan.model_json_schema()},task_id,
                    validator=lambda raw:validate_sourced_spec(asset_sheets.CharacterPlan,raw,passages,'characters'))
                checkpoint('character_plan',people.model_dump())
            else:people=validate_sourced_spec(asset_sheets.CharacterPlan,people,passages,'characters')
            identity={'visual_style':style.visual_style.model_dump(),'characters':[p.model_dump() for p in people.characters]}
            checkpoint('identity_plan',identity)
        identity=validate_spec(asset_sheets.IdentityPlan,identity)
        for person in identity.characters:source_refs(person.source_ref,passages)
        materials=checkpoints.get('wardrobe_scene_plan')
        if materials is None:
            costumes=checkpoints.get('costume_plan')
            if costumes is None:
                costumes,_=call_node(chat_json,'costume_spec',{**context,'visual_style':identity.visual_style.model_dump(),
                    'locked_characters':[p.model_dump() for p in identity.characters],'schema':asset_sheets.CostumePlan.model_json_schema()},task_id,
                    validator=lambda raw:validate_costume_plan(raw,passages,identity.characters))
                checkpoint('costume_plan',costumes.model_dump())
            else:costumes=validate_costume_plan(costumes,passages,identity.characters)
            scenes=checkpoints.get('scene_plan')
            if scenes is None:
                schema=asset_sheets.ScenePlan.model_json_schema()
                schema['properties']['scenes']['maxItems']=asset_sheets.MAX_MATERIALS-len(costumes.costumes)
                scenes,_=call_node(chat_json,'scene_spec',{**context,'visual_style':identity.visual_style.model_dump(),'schema':schema},task_id,
                    validator=lambda raw:validate_sourced_spec(asset_sheets.ScenePlan,raw,passages,'scenes'))
                checkpoint('scene_plan',scenes.model_dump())
            else:scenes=validate_sourced_spec(asset_sheets.ScenePlan,scenes,passages,'scenes')
            materials={'items':[i.model_dump() for i in costumes.costumes]+[i.model_dump() for i in scenes.scenes]}
            checkpoint('wardrobe_scene_plan',materials)
        materials=validate_spec(asset_sheets.WardrobeScenePlan,materials)
        raw={'visual_style':identity.visual_style.model_dump(),'items':[p.model_dump() for p in identity.characters]+[i.model_dump() for i in materials.items]}
        checkpoint('raw_design',raw)
    plan,bindings=validate_design(raw,passages,payload.get('asset_schema'))
    # Every character answered the costume stage, but the empty-clothing mode has no sheet of its
    # own: rendering one would fabricate a garment for a natural body. Those records are kept as
    # decisions and only the real sheets are rendered.
    renderable=[(item,refs) for item,refs in zip(plan.items,bindings) if not is_bare_costume(item)]
    bare_costumes=[item for item in plan.items if is_bare_costume(item)]
    prompt_plan=plan.model_copy(update={'items':[item for item,_ in renderable]})
    prompts,prompt_fallbacks,prompt_nodes=write_asset_prompts(task_id,payload,prompt_plan) if payload.get('skill_pipeline') else ({},set(),{})
    with Session.begin() as db:
        current=db.get(Task,task_id)
        if current.status!='running':return
        get_project(db,payload['creative_id']);r=get_run(db,payload['creative_id'])
        if r.data.get('watch')!=task_id:raise ProviderError('美术设计任务已更新，旧结果不再使用。')
        if r.data.get('items'):return
        items=[]
        for index,(item,refs) in enumerate(renderable):
            prompt=prompts.get(index) or asset_sheets.compose_prompt(plan.visual_style,item)
            metadata={'asset_schema':asset_sheets.VERSION,'asset_kind':asset_sheets.KINDS[item.role],
                'asset_role':item.role,'asset_spec':item.model_dump(),'visual_style':plan.visual_style.model_dump(),
                'character_key':getattr(item,'character_id',getattr(item,'character_ref',None)),
                'costume_key':getattr(item,'costume_id',None),
                'prompt_source':'validated_render_contract' if index in prompt_fallbacks else prompt_nodes.get(index,'asset_prompts')}
            if index in prompt_nodes and index not in prompt_fallbacks:
                from .skill_runtime import public
                metadata['node_skill']=public(current.payload['node_skill_pins'][prompt_nodes[index]])
            t=Task(id=uid('image'),kind='image',payload={'mode':'live','creative_id':payload['creative_id'],'title':item.name,'prompt':prompt,**metadata})
            saved={**item.model_dump(),'design':asset_sheets.description(item),'prompt':prompt,**metadata}
            db.add(t);items.append({**saved,'source_refs':refs,'source_quote':'\n'.join(passages[ref] for ref in refs),'task_id':t.id})
        r=get_run(db,payload['creative_id']);r.data={**r.data,'visual_style':plan.visual_style.model_dump(),
            'visual_language':asset_sheets.style_prompt(plan.visual_style),'asset_schema':asset_sheets.VERSION,'items':items,'looks':[],
            'costume_records':[{'costume_id':item.costume_id,'character_ref':item.character_ref,'name':item.name,
                'mode':item.mode,'description':asset_sheets.description(item),
                'source_quote':'\n'.join(passages[ref] for ref in refs)}
                for item,refs in zip(plan.items,bindings) if item.role=='costume'],
            'bare_costumes':[{'costume_id':item.costume_id,'character_ref':item.character_ref,'name':item.name,
                'mode':item.mode,'description':asset_sheets.description(item)} for item in bare_costumes],
            'stage':'assets_review'}
        current.result={**current.result,'replaced_assets':payload.get('replaces_assets',[]),
                        'prompt_fallbacks':sorted(prompt_fallbacks)}
        owner=current.owner
    from .task_activity import Activity,preview
    Activity(task_id,owner,'人物身份、服装与场景规格','model',new_call=False).save(
        'ready','人物身份、服装与场景规格已通过校验',preview(json.dumps(raw,ensure_ascii=False)),force=True)


def asset_prompt_node(item):
    return 'character_prompts' if item.role=='character' else 'asset_prompts'


def is_bare_costume(item):
    """The empty-clothing mode documents a costume decision without rendering a sheet."""
    return getattr(item,'role',None)=='costume' and getattr(item,'mode','garment')=='bare'


def write_asset_prompts(task_id,payload,plan):
    """Role-specific prompt nodes see only approved static contracts, never prose."""
    from .environment import model_config
    cfg=model_config();prompts={};fallbacks=set();prompt_nodes={}
    with Session() as db:cached=dict(get_run(db,payload['creative_id']).data.get('image_prompt_batches',{}))
    groups=(
        ('character_prompts',[index for index,item in enumerate(plan.items) if item.role=='character']),
        ('asset_prompts',[index for index,item in enumerate(plan.items) if item.role!='character']),
    )
    # Small role-homogeneous batches keep character expertise out of costume/set prompts.
    for node,indexes in groups:
        for offset in range(0,len(indexes),asset_sheets.PROMPT_BATCH_SIZE):
            selected=indexes[offset:offset+asset_sheets.PROMPT_BATCH_SIZE]
            assets=[{'asset_index':index,'role':plan.items[index].role,
                     'render_contract':asset_sheets.compose_prompt(plan.visual_style,plan.items[index])}
                    for index in selected]
            key=node+':'+','.join(str(index) for index in selected)
            target={'provider':cfg.get('IMAGE_PROVIDER'),'model':cfg.get('IMAGE_MODEL'),'image_size':'1024x1024'}
            request_hash=hashlib.sha256(json.dumps([node,assets,target],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            prior=cached.get(key)
            if prior and prior.get('request_sha256')!=request_hash:
                raise ProviderError('已保存的提示词与当前素材规格或模型配置不一致，请创建新修订，未重复提交。')
            fallback_reason=prior.get('fallback_reason') if prior else None
            raw=prior.get('response') if prior else None
            batch=None
            if raw is None and fallback_reason is None:
                schema=asset_sheets.AssetPromptBatch.model_json_schema()
                schema['properties']['items'].update(minItems=len(assets),maxItems=len(assets))
                expected={asset['asset_index'] for asset in assets}
                def prompt_contract(value):
                    result=validate_spec(asset_sheets.AssetPromptBatch,value)
                    indexes=[item.asset_index for item in result.items]
                    if len(indexes)!=len(expected) or set(indexes)!=expected:
                        raise ModelOutputError('图片提示词必须逐项且仅对应本批素材编号。')
                    return result
                try:
                    batch,_=call_node(chat_json,node,{'assets':assets,
                        'target':target,
                        'schema':schema},task_id,validator=prompt_contract)
                    raw=batch.model_dump()
                except ModelOutputError as exc:
                    raw={};fallback_reason=str(exc)
                with Session.begin() as db:
                    run=get_run(db,payload['creative_id'])
                    if run.data.get('watch')!=task_id:raise ProviderError('提示词任务已更新。')
                    entry={'node':node,'request_sha256':request_hash,'response':raw}
                    if fallback_reason:entry['fallback_reason']=fallback_reason
                    cached={**cached,key:entry};run.data={**run.data,'image_prompt_batches':cached}
            if fallback_reason is None:
                try:
                    if batch is None:
                        batch=validate_spec(asset_sheets.AssetPromptBatch,raw)
                except ProviderError as exc:fallback_reason=str(exc)
            batch_fallbacks=[]
            for asset in assets:
                matches=[item.prompt for item in batch.items if item.asset_index==asset['asset_index']] if batch else []
                if len(matches)==1:
                    prompts[asset['asset_index']]=matches[0]
                    prompt_nodes[asset['asset_index']]=node
                else:
                    prompts[asset['asset_index']]=asset['render_contract']
                    fallbacks.add(asset['asset_index'])
                    batch_fallbacks.append(asset['asset_index'])
            if batch_fallbacks and (not prior or prior.get('fallback_indices')!=batch_fallbacks or fallback_reason and prior.get('fallback_reason')!=fallback_reason):
                with Session.begin() as db:
                    run=get_run(db,payload['creative_id'])
                    if run.data.get('watch')!=task_id:raise ProviderError('提示词任务已更新。')
                    entry={**cached[key],'fallback_indices':batch_fallbacks}
                    if fallback_reason:entry['fallback_reason']=fallback_reason
                    cached={**cached,key:entry};run.data={**run.data,'image_prompt_batches':cached}
    return prompts,fallbacks,prompt_nodes


def write_revised_asset_prompt(task_id,style,item):
    """Compile one revised static specification into a fresh text-to-image prompt."""
    from .environment import model_config
    cfg=model_config()
    assets=[{'asset_index':0,'role':item.role,'render_contract':asset_sheets.compose_prompt(style,item)}]
    schema=asset_sheets.AssetPromptBatch.model_json_schema()
    schema['properties']['items'].update(minItems=1,maxItems=1)
    def prompt_contract(raw):
        result=validate_spec(asset_sheets.AssetPromptBatch,raw)
        if len(result.items)!=1 or result.items[0].asset_index!=0:
            raise ModelOutputError('重做提示词没有准确对应当前素材。')
        return result
    node=asset_prompt_node(item)
    batch,_=call_node(chat_json,node,{'assets':assets,
        'target':{'provider':cfg.get('IMAGE_PROVIDER'),'model':cfg.get('IMAGE_MODEL'),'image_size':'1024x1024'},
        'schema':schema},task_id,validator=prompt_contract)
    return batch.items[0].prompt,node

def image_asset(db,tid):
    t=db.get(Task,tid);a=db.get(Record,t.result.get('asset_id','')) if t else None
    if t and t.status in ('failed','needs_review'):
        from .image_errors import task_error,error_message
        error=task_error(t)
        reason=error_message(error) if error else t.message
        raise HTTPException(409,f'「{t.payload.get("title") or "图片"}」尚未完成：{reason}')
    if not t or t.status!='completed' or not a:raise HTTPException(409,'图片尚未完成，请等待或处理失败任务。')
    return a

def approve_images(db,ids,automatic=False):
    for aid in ids:
        a=db.get(Record,aid)
        if not a:raise HTTPException(409,'图片不存在。')
        if a.data.get('status')!='approved':a.data={**a.data,'status':'approved'};a.version+=1
        db.add(Record(id=uid('audit'),kind='audit',data={'target':aid,'action':'workflow_image_accepted' if automatic else 'creative_image_approved','note':'工作流依据已锁定参考图继续制作；未宣称人工视觉审核' if automatic else '用户在图片审核页确认满意'}))

@router.get('/{pid}')
def workspace(pid:str):
    with Session() as db:
        get_project(db,pid);r=db.get(Record,'creative_'+pid)
        if not r:return None
        items=[]
        for i in r.data.get('items',[]):
            t=db.get(Task,i['task_id']);a=db.get(Record,t.result.get('asset_id','')) if t else None
            items.append({**i,'task':task_dict(t) if t else None,'asset':record_dict(a) if a else None})
        watch=db.get(Task,r.data.get('watch',''))
        result={**record_dict(r),'items':items,'task':task_dict(watch) if watch else None}
    result['production']=production.workspace(pid)
    return result

def prepare_reference_inputs(pid):
    """Bind reviewed base sheets; any needed local stitching is decided per storyboard shot later."""
    with Session.begin() as db:
        run=get_run(db,pid);data=dict(run.data)
        if data.get('asset_schema')!=asset_sheets.VERSION:raise HTTPException(409,'请先将基础素材更新为独立人物身份、服装和场景图。')
        rows={item['task_id']:image_asset(db,item['task_id']) for item in data['items']}
        approve_images(db,[row.id for row in rows.values()],automatic=False)
        people={item['character_id']:rows[item['task_id']].id for item in data['items'] if item['role']=='character'}
        costumed={item['character_ref'] for item in data['items'] if item['role']=='costume'}
        # Empty-clothing decisions are not rendered sheets, so they are carried in separately and
        # recorded on the character so the storyboard never asks for a garment that does not exist.
        bare={record['character_ref']:record for record in data.get('bare_costumes',[])}
        assets={}
        for item in data['items']:
            asset=rows[item['task_id']]
            spec={'role':item['role'],'name':item['name'],'notes':str(item.get('design') or item.get('facts') or '使用这张已确认的基础参考图')[:1500]}
            if item['role']=='character':
                spec.update(identity_asset_id=asset.id,requires_costume=item['character_id'] in costumed)
                decision=bare.get(item['character_id'])
                if decision:
                    spec.update(clothing_mode='bare',clothing_note=decision['description'][:600])
                elif not spec['requires_costume']:
                    # No garment and no empty-clothing decision means the costume stage never
                    # answered for this character; refuse instead of silently drawing base clothes.
                    raise HTTPException(409,f'「{item["name"]}」既没有服装记录也没有空衣服模式记录，请先重新运行美术设计节点。')
                else:
                    spec['clothing_mode']='garment'
            elif item['role']=='costume':
                identity=people.get(item.get('character_ref'))
                if not identity:raise HTTPException(409,'服装缺少对应的人物身份图，请先检查基础素材。')
                spec['identity_asset_id']=identity
            assets[asset.id]=spec
        source_assets={task_id:{'asset_id':asset.id,'asset_version':asset.version} for task_id,asset in rows.items()}
    current=prep.get(pid)['config']
    prep.save(pid,prep.Setup(expected_version=current['version'] if current else 0,style=data['visual_language'],assets=assets,source_assets=source_assets))
    prep.approve_references(pid,prep.get(pid)['stamp'])
    save_run(pid,stage='references_ready',reference_inputs='base_sheets_conditional_stitching_ready')


def start_storyboard_stage(pid):
    with Session() as db:
        version=db.get(Record,pid).version
        saved=get_run(db,pid).data
        retry_feedback=saved.get('storyboard_retry_feedback')
        reuse_chunks=bool(saved.get('storyboard_reuse_chunks'))
    child=director.start_storyboard(pid,director.BoardStart(
        version=version,confirm_paid=True,retry_feedback=retry_feedback,reuse_saved_chunks=reuse_chunks))
    with Session.begin() as db:
        w=Task(id=uid('watch'),kind='creative_watch',payload={'mode':'live','creative_id':pid,'child':child['task']['id'],'step':'board'})
        db.add(w);r=get_run(db,pid)
        data={key:value for key,value in r.data.items() if key not in ('storyboard_retry_feedback','storyboard_reuse_chunks')}
        r.data={**data,'stage':'storyboarding','watch':w.id}

def get_status(pid):
    with Session() as db:return get_run(db,pid).data['stage']

def review_notes(project):
    """The text pre-review's findings, kept for the author and never fed back as instructions.

    A reviewer with no ground truth reports creative opinions as well as real problems. They are
    worth showing and useless as "必须修正" instructions, so they are recorded here and displayed
    beside the board instead of being handed to the storyboard model.
    """
    if not project:return None
    review=project.data.get('review') or {}
    issues=[str(issue).strip() for issue in (review.get('issues') or []) if str(issue).strip()]
    if review.get('approved') is not False and not issues:return None
    return {'issues':issues,'continuity':review.get('continuity'),'dramatic_logic':review.get('dramatic_logic'),
            'editability':review.get('editability'),'production_feasibility':review.get('production_feasibility'),
            'approved':bool(review.get('approved')),'at':time.time()}

def run_watch(task_id,payload):
    """Finish one storyboard node: record the text pre-review, then hand the film to rendering.

    The pre-review is a text opinion with no ground truth, so it never sends the film back. It used
    to raise a retry (twice) and then hand the reviewer's creative notes to the storyboard model as
    "必须逐条修正", which made the model argue with the reviewer and rewrite the whole film each
    time. Its findings are recorded for the author instead; re-running the node is an explicit
    author action.
    """
    pid=payload['creative_id']
    with Session() as db:
        get_project(db,pid);child=db.get(Task,payload['child'])
        if child.status in BUSY:return False
        if child.status!='completed':raise ProviderError('自动分镜未完成，请查看导演诊断。已有图片保留。')
        p=db.get(Record,pid)
        notes=review_notes(p)
        version=p.version;board=director.Board.model_validate(p.data['board'])
    if notes is not None:
        with Session.begin() as db:
            run=get_run(db,pid)
            run.data={**run.data,'storyboard_review_notes':notes}
            db.add(Record(id=uid('audit'),kind='audit',data={'target':pid,'action':'storyboard_review_recorded',
                'approved':notes['approved'],'issues':notes['issues'][:8]}))
    # Format and semantic text checks succeeded; this is not an image review.
    approved=director.approve(pid,director.Approve(version=version,confirm=True,note='系统分镜结构与模型文本预审通过；后续图片仍须管理员审核'))
    pre=prep.get(pid)['config']
    old=visual.get_config(pid)['config']
    visual.save(pid,visual.Visual(expected_version=old['version'] if old else 0,director_version=approved['version'],style=pre['style'],bindings={s.id:s.assets for s in board.shots},states={s.id:s.continuity_in+' → '+s.continuity_out for s in board.shots},approved=True))
    with Session() as db:split=get_run(db,pid).data.get('production_split',False)
    save_run(pid,stage='storyboard_ready')
    if not split:start_reference_videos(pid)
    return True

def start_reference_videos(pid):
    """Generate every shot video directly from the reviewed project reference images."""
    run=production.workspace(pid)
    for shot in run['shots']:
        if not shot['video_task']:production.generate_video(pid,shot['shot']['id'],production.Command(version=run['version'],confirm_paid=True))
    save_run(pid,stage='videos_review')

class Feedback(BaseModel):
    task_id:str
    text:str=Field(min_length=2,max_length=1500)
    confirm_paid:bool=False
@router.post('/{pid}/feedback')
def feedback(pid:str,body:Feedback):
    # Revising a prompt calls the text model; the regenerated picture or clip is gated separately
    # when its own task is submitted.
    paid(body,'llm')
    with Session.begin() as db:
        get_project(db,pid);r=get_run(db,pid);no_active(db,pid)
        if r.data['stage']=='published':raise HTTPException(409,'已发布作品请归档并新建版本修改。')
        old=db.get(Task,body.task_id)
        if not old or old.kind not in ('image','video') or not any(old.payload.get(k)==pid for k in ('creative_id','director_id','preproduction_id')):raise HTTPException(422,'不是当前作品的素材。')
        if old.status not in ('completed','failed','needs_review'):raise HTTPException(409,'任务仍在运行。')
        # Upstream changes require rebuilding downstream versions.
        if old.payload.get('creative_id')==pid and r.data['stage']!='assets_review':raise HTTPException(409,'角色设定已用于后续制作；整体改换请归档重做，避免污染已审场景。')
        if old.payload.get('creative_id')==pid and old.id not in {item['task_id'] for item in r.data['items']}:
            raise HTTPException(409,'该图片已有更新版本，请刷新后修改当前图片。')
        t=Task(id=uid('revise'),kind='creative_revision',payload={'mode':'live','creative_id':pid,'target':old.id,'feedback':body.text})
        db.add(t);db.add(Record(id=uid('audit'),kind='audit',data={'target':old.id,'action':'creative_feedback','note':body.text,'task_id':t.id}));r.data={**r.data,'watch':t.id};r.version+=1;db.flush();return task_dict(t)

class Revision(BaseModel):
    prompt:str=Field(min_length=5,max_length=5000)

def run_revision(task_id,p):
    with Session() as db:
        get_project(db,p['creative_id']);old=db.get(Task,p['target'])
        if not old:raise ProviderError('需要重做的素材不存在。')
        old_payload=dict(old.payload);kind=old.kind
    updated_spec=None;prompt_node=None
    if kind=='image' and old_payload.get('creative_id') and not old_payload.get('director_id'):
        if old_payload.get('asset_schema')!=asset_sheets.VERSION or not old_payload.get('asset_spec'):
            raise ProviderError('旧版人物与服装尚未分离，请先重做本轮素材。')
        original_spec=old_payload['asset_spec'];schema=asset_sheets.MODELS[original_spec['role']]
        def revision_contract(raw):
            result=validate_spec(schema,raw);values=result.model_dump()
            for key in ('role','name','source_ref','facts','character_id','character_ref','costume_id'):
                if values.get(key)!=original_spec.get(key):
                    raise ModelOutputError('修改不能更换人物身份、服装归属或原文依据。')
            return result
        updated_spec,_=call_node(chat_json,{'character':'identity_revision','costume':'costume_revision','scene':'scene_revision'}[original_spec['role']],
            {'existing_spec':original_spec,'feedback':p['feedback'],'schema':schema.model_json_schema()},task_id,
            validator=revision_contract)
        prompt,prompt_node=write_revised_asset_prompt(task_id,old_payload['visual_style'],updated_spec)
    else:
        revision_node='video_revision' if kind=='video' else 'shot_revision'
        revision,_=call_node(chat_json,revision_node,
            {'original':old_payload['prompt'],'feedback':p['feedback'],'schema':Revision.model_json_schema()},task_id,
            validator=Revision.model_validate)
        prompt=revision.prompt
    with Session.begin() as db:
        current=db.get(Task,task_id)
        if current.status!='running':return
        get_project(db,p['creative_id']);r=get_run(db,p['creative_id'])
        payload={**old_payload,'prompt':prompt,'revision_of':p['target'],'feedback':p['feedback'],
            'revision_instruction':p['feedback'],'regeneration_mode':'full_prompt'}
        if updated_spec is not None:
            # Author feedback on a base asset always starts a new text-to-image request.
            # Reference inputs from any historical edit must never leak into the new task.
            for key in ('reference_media','reference_ids','reference_assets','image_model','image_inference_steps',
                        'style_reference','reference_roles','edit_mode','edit_instruction','rendering_style'):
                payload.pop(key,None)
            from .skill_runtime import public
            payload.update(asset_spec=updated_spec.model_dump(),input_mode='text_to_image',
                prompt_source=prompt_node,node_skill=public(current.payload['node_skill_pins'][prompt_node]))
        else:
            from .skill_runtime import public
            payload['node_skill']=public(current.payload['node_skill_pins'][revision_node])
            if kind=='video' and isinstance(payload.get('input_snapshot'),dict):
                payload['input_snapshot']={**payload['input_snapshot'],'prompt':prompt}
        t=Task(id=uid(kind),kind=kind,payload=payload);db.add(t)
        if updated_spec is not None:
            prep.invalidate_downstream_references(db,p['creative_id'],'基础素材已按意见重做',t.id)
        changes={**updated_spec.model_dump(),'design':asset_sheets.description(updated_spec),'asset_spec':updated_spec.model_dump(),'prompt':prompt} if updated_spec is not None else {}
        items=[{**i,**changes,'task_id':t.id} if i['task_id']==p['target'] else i for i in r.data['items']]
        stage=r.data['stage']
        if kind=='image' and old_payload.get('director_id'):
            raise ProviderError('当前流程直接从已审核参考图生成视频，不再修改镜头参考图；请修改基础参考图或重新生成视频。')
        r.data={**r.data,'items':items,'stage':stage};r.version+=1
        current.result={**current.result,'task_id':t.id}

class Publish(BaseModel):
    confirm:bool=False
@router.post('/{pid}/publish')
def publish(pid:str,body:Publish):
    if not body.confirm:raise HTTPException(422,'请看过成片后确认发布。')
    run=production.workspace(pid)
    for s in run['shots']:
        if not s['clip']:raise HTTPException(409,'还有视频未完成。')
    for s in run['shots']:
        if not s['clip'].get('locked'):production.approve_clip(pid,s['shot']['id'],production.Trim(version=run['version'],start=0,end=min(s['shot']['edit_seconds'],s['clip']['duration']),confirm_visual=True))
    result=production.publish(pid,production.Publish(version=run['version'],confirm=True));save_run(pid,stage='published')
    from .skill_runtime import public,snapshot
    with Session.begin() as db:
        key='review_skill_'+result['id']
        if not db.get(Record,key):db.add(Record(id=key,kind='audit',data={'target':result['id'],'action':'author_film_review','node_skill':public(snapshot('film_review'))}))
    return result
