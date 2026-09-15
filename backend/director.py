"""Brainstorm-fiction directing: grounded treatment, shot design, independent review."""
from typing import Literal
import re
import time
import unicodedata
from pydantic import AliasChoices,BaseModel,Field,ValidationError
from fastapi import APIRouter,HTTPException
from sqlalchemy import select
from .db import HISTORY_LIMIT, Record, Session, Task, record_dict, task_dict, uid
from .providers import paid_gate,settings,chat_json,ProviderError,ModelOutputError
from .skill_runtime import call_node
from . import segments

VERSION='brainstorm-1.0'
router=APIRouter(prefix='/api/director',tags=['director'])

class Rule(BaseModel):
    source_ref:str=Field(default='',max_length=20,description='对应原文 P 编号；镜头默认按 P 编号非递减排列，保持原著因果顺序。')
    rule:str=Field(min_length=1,max_length=500)
    quote:str=Field(min_length=2,max_length=300)
    consequence:str=Field(min_length=1,max_length=500)

class Treatment(BaseModel):
    premise:str=Field(min_length=1,max_length=600)
    dramatic_question:str=Field(min_length=1,max_length=400)
    protagonist_goal:str=Field(min_length=1,max_length=400)
    rules:list[Rule]=Field(min_length=1,max_length=8)
    excerpt_scope:str=Field(min_length=1,max_length=600)
    visual_strategy:str=Field(min_length=1,max_length=1000)
    information_strategy:str=Field(min_length=1,max_length=1000)
    boundaries:list[str]=Field(min_length=1,max_length=10)

class Shot(BaseModel):
    source_ref:str=Field(default='',max_length=20)
    id:str=Field(pattern=r'^S[0-9]{2}$')
    segment_id:str=Field(default='',max_length=12,description='本镜所属的微小说片段编号；由调用方在合并分块结果时写入。')
    scene:str=Field(min_length=1,max_length=160)
    purpose:Literal['hook','setup','rule','escalation','reveal','reaction','payoff','bridge']=Field(
        description='首次建立或发现信息用 hook/setup/rule/escalation；reveal/payoff 仅用于回应前序镜头已经铺垫的信息，并必须填写 setup_ids。',
    )
    source_quote:str=Field(min_length=2,max_length=300)
    dramatic_action:str=Field(min_length=1,max_length=600)
    size:Literal['EWS','WS','MS','MCU','CU','ECU','INSERT']
    camera:str=Field(min_length=1,max_length=300)
    composition:str=Field(min_length=1,max_length=400)
    blocking:str=Field(min_length=1,max_length=400)
    continuity_in:str=Field(min_length=1,max_length=400)
    continuity_out:str=Field(min_length=1,max_length=400)
    viewer_knows:str=Field(min_length=1,max_length=400)
    character_knows:str=Field(min_length=1,max_length=400)
    withhold:str=Field(min_length=1,max_length=300)
    setup_ids:list[str]=Field(
        default_factory=list,
        max_length=8,
        description='所回应该信息的前序镜号。reveal/payoff 必填且只能引用本镜之前的 Sxx；若 continuity_in 明写承接某镜，须同步填写该镜号。',
    )
    edit_seconds:float=Field(ge=1,le=15)
    generation_seconds:int=Field(ge=5,le=15)
    dialogue:str=Field(max_length=400)
    sound:str=Field(min_length=1,max_length=400)
    transition:str=Field(min_length=1,max_length=300)
    reference_prompt:str=Field(min_length=5,max_length=1500,validation_alias=AliasChoices('reference_prompt','first_frame'),description='镜头构图说明：景别、机位、站位、视线、光线与人物服装场景外观，直接作为参考图生视频的构图依据，不限定视频起止帧')
    motion_prompt:str=Field(min_length=5,max_length=1500)
    assets:list[str]=Field(
        min_length=1,
        max_length=12,
        description='从 preproduction.asset_binding_contract 复制完整语义素材 ID；不要为参考图数量上限省略人物或服装，程序会把它们原样附给视频模型。',
    )
    generation_risk:str=Field(min_length=1,max_length=400)

class Board(BaseModel):
    title:str=Field(min_length=1,max_length=120)
    scope_note:str=Field(min_length=1,max_length=600)
    shots:list[Shot]=Field(min_length=1,max_length=segments.MAX_TOTAL_SHOTS,
        description='完整分镜按微小说片段分块生成后再合并；单次模型输出只负责一个片段。')

class Review(BaseModel):
    approved:bool
    issues:list[str]=Field(default_factory=list,max_length=20)
    continuity:str
    dramatic_logic:str
    editability:str
    production_feasibility:str


class ShotPrompt(BaseModel):
    id:str=Field(pattern=r'^S[0-9]{2}$')
    reference_prompt:str=Field(min_length=5,max_length=1500,validation_alias=AliasChoices('reference_prompt','first_frame'),description='单镜静态构图说明，配合已绑定项目参考图直接生成视频；不重复统一画风，不指定视频首尾帧')
    motion_prompt:str=Field(min_length=5,max_length=1500)


class ShotPromptBatch(BaseModel):
    shots:list[ShotPrompt]=Field(min_length=1,max_length=segments.MAX_SHOTS_PER_SEGMENT+2)

def normalized_quote(text):
    # Typography-only tolerance; words, negations and sentence order remain intact.
    return ''.join(c for c in unicodedata.normalize('NFKC',text).translate(str.maketrans({'“':'"','”':'"','「':'"','」':'"','‘':"'",'’':"'"})) if not c.isspace())

def quote_exists(quote,content):
    return bool(normalized_quote(quote)) and normalized_quote(quote) in normalized_quote(content)

def source_passages(content):
    return {f'P{i+1:03}':content[start:start+240] for i,start in enumerate(range(0,len(content),240))}

def bind_sources(items,passages,field,content=None):
    """Attach the cited passage, and verify the model's own quote was not invented.

    Replacing the quote with the passage happens after this check; doing it first is what made the
    "原文依据无法定位" rule unable to fire, because every fabricated quote had already been replaced
    by the real text before validation ran.
    """
    changes=[]
    for item in items:
        if item.source_ref:
            if item.source_ref not in passages:raise ProviderError(f'原文依据编号 {item.source_ref} 不存在，已保存草稿。')
            original=getattr(item,'source_quote','') or ''
            if content:
                if not quote_exists(original,content):
                    head=normalized_quote(original)[:8]
                    if len(head)<8 or head not in normalized_quote(content):
                        raise ProviderError(f'{getattr(item,"id","")} 的原文依据引文在原文中找不到，可能被改写或编造：'
                                            f'“{original[:60]}”。请照抄原文，或改为引用本片段中真实存在的句子。')
                    changes.append({'shot_id':getattr(item,'id',''),'action':'source_quote_narrowed',
                                    'source_ref':str(item.source_ref).upper(),'model_quote':original[:80]})
                elif normalized_quote(original) not in normalized_quote(passages[item.source_ref]):
                    changes.append({'shot_id':getattr(item,'id',''),'action':'source_quote_rebound',
                                    'source_ref':str(item.source_ref).upper(),'model_quote':original[:80]})
            setattr(item,field,passages[item.source_ref])
    return changes

def repair_board_causality(board):
    """Copy explicit continuity references into omitted structured causality links."""
    repaired=board.model_copy(deep=True);changes=[];prior_ids=[]
    for shot in repaired.shots:
        if shot.purpose in ('reveal','payoff') and not shot.setup_ids:
            explicit=list(dict.fromkeys(ref.upper() for ref in re.findall(r'(?<![A-Z0-9])S\d{2}(?!\d)',shot.continuity_in,re.I)
                                    if ref.upper() in prior_ids))
            if explicit:
                shot.setup_ids=explicit
                changes.append({'shot_id':shot.id,'action':'bind_explicit_setup_reference','setup_ids':explicit})
        prior_ids.append(shot.id)
    return repaired,changes


def check_board(board,content,external_ids=(),allow_opening_continuation=False):
    """Structural and source-text checks; a segment chunk may continue the previous chunk."""
    known=set(external_ids)
    issues=[];seen=set();prior_ids=[];latest_source=None;latest_source_shot=None
    for index,shot in enumerate(board.shots):
        if shot.id in seen:issues.append(f'{shot.id} 镜号重复')
        if not quote_exists(shot.source_quote,content):issues.append(f'{shot.id} 原文依据无法定位')
        invalid=[setup_id for setup_id in shot.setup_ids if setup_id not in seen and setup_id not in known]
        if invalid:
            available='、'.join([*prior_ids,*sorted(known)]) or '当前没有前序镜头'
            issues.append(f'{shot.id} 铺垫必须引用前序镜头；无效 setup_ids：{"、".join(invalid)}；可引用：{available}')
        if shot.purpose in ('reveal','payoff') and not shot.setup_ids and not (allow_opening_continuation and index==0):
            available='、'.join([*prior_ids,*sorted(known)]) or '当前没有前序镜头'
            issues.append(
                f'{shot.id} 揭示或回收缺少前序铺垫：若它回应已建立的信息，请在 setup_ids 填对应前序镜号（可引用：{available}）；'
                '若它是首次建立或发现信息，请把 purpose 改为 hook、setup、rule 或 escalation。'
            )
        source_match=re.fullmatch(r'P(\d+)',shot.source_ref,re.I)
        if source_match:
            source_index=int(source_match.group(1))
            if latest_source is not None and source_index<latest_source:
                issues.append(
                    f'{shot.id} 原文顺序倒退：{shot.source_ref.upper()} 排在 '
                    f'{latest_source_shot}（P{latest_source:03}）之后；请按 P 编号非递减排列镜头，保持原著因果顺序。'
                )
            else:latest_source=source_index;latest_source_shot=shot.id
        if shot.edit_seconds>shot.generation_seconds:issues.append(f'{shot.id} 剪辑时长超过生成素材时长')
        seen.add(shot.id);prior_ids.append(shot.id)
    return issues


def board_validation_errors(exc,raw):
    """Turn schema failures into retry feedback that tells the model what to change."""
    errors=[]
    shots=raw.get('shots',[]) if isinstance(raw,dict) and isinstance(raw.get('shots'),list) else []
    for error in exc.errors(include_input=False,include_url=False):
        location=error['loc'];reason=error['msg']
        if location==['shots'] and error['type'] in ('too_long','list_too_long'):
            reason=('本片段镜头数超过调用方为本片段分配的镜头上限：请合并次要动作或删除重复镜头，'
                    '并保持每个片段只讲一件事。')
        if (len(location)>=3 and location[0]=='shots' and isinstance(location[1],int)
                and location[-1]=='assets' and error['type'] in ('too_long','list_too_long')):
            shot=shots[location[1]] if location[1]<len(shots) and isinstance(shots[location[1]],dict) else {}
            shot_id=shot.get('id') or f'第 {location[1]+1} 镜'
            asset_count=len(shot.get('assets',[])) if isinstance(shot.get('assets'),list) else '超过上限'
            reason=(f'{shot_id} 的 assets 列出了 {asset_count} 项，超过分镜语义素材上限 12 项。'
                    '请缩小单镜内容或拆镜，并继续完整绑定每个角色的身份、服装与对应场景。')
        errors.append({'field':'.'.join(str(x) for x in location),'reason':reason,'type':error['type']})
    return errors


def chunk_schema(shot_budget):
    """One storyboard chunk plans only its own segment, inside that segment's shot budget."""
    schema=Board.model_json_schema()
    fields=schema['properties']['shots']
    fields['minItems']=1;fields['maxItems']=shot_budget
    fields['description']=f'本片段最多 {shot_budget} 镜。只规划本片段，不要写其它片段的镜头，也不要把整篇故事合成一次输出。'
    schema['description']='单个剧情片段的镜头计划；调用方会按片段顺序合并并统一编号。'
    return schema


def segment_stage(payload,passages,task_id,save_stage,treatment):
    """Cut the micro-fiction into ordered segments, or reuse the saved cut."""
    saved=payload.get('segments')
    if saved:
        plan=segments.plan_from_saved(saved)
        issues=segments.plan_issues(plan,passages)
        if issues:raise ProviderError('已保存的微小说片段与原文不一致：'+'；'.join(issues[:3]))
        return plan,segments.plan_dump(plan,passages),'saved'
    shot_limit,limit_note=shot_budget_limit()
    if shot_limit is not None and shot_limit<segments.MIN_AFFORDABLE_SHOTS:
        raise ProviderError('共享体验额度今天不足以完成一个短片（'+limit_note+'）。请改用自己的 Key，或明天再试。')
    save_stage('phase','正在把微小说切割成有序片段',28)
    attempts=[]
    def plan_contract(raw):
        try:plan=segments.validate_plan_output(raw,passages,shot_limit)
        except ModelOutputError as exc:
            attempts.append({'raw':raw,'error':str(exc)})
            save_stage('segment_diagnostics',{'raw':raw,'error':str(exc),'attempts':list(attempts)},28)
            raise
        return plan
    request={'source':payload['source'],'source_passages':passages,'brief':payload.get('brief',''),
             'treatment':treatment.model_dump(),'schema':segments.segment_schema()}
    if shot_limit is not None:request['shot_limit']=shot_limit
    plan,_=call_node(chat_json,'story_segments',request,task_id,validator=plan_contract)
    cut=segments.plan_dump(plan,passages)
    save_stage('segments',cut,32)
    return plan,cut,'generated'


def shot_budget_limit():
    """Cap the whole-film shot count to what this account's beans can still pay for."""
    try:
        from .model_access import affordable_shot_budget
    except ImportError:return None,''
    decision=affordable_shot_budget()
    if decision is None:return None,''
    return decision


def chunk_issues(chunk,segment,passages,preproduction,expected_ids=None,earlier_ids=(),content=None):
    """Re-validate a saved chunk before it is reused without another paid call."""
    issues=[];text=segments.segment_text(segment,passages)
    if expected_ids is not None and [shot.id for shot in chunk.shots]!=list(expected_ids):
        issues.append('镜号已不符合整片连续编号：应为 '+'、'.join(expected_ids)+'。')
    missing=[shot.id for shot in chunk.shots if shot.source_ref and shot.source_ref.upper() not in segment.source_refs]
    if missing:issues.append('source_ref 不在本片段范围：'+ '、'.join(missing))
    try:bind_sources(chunk.shots,passages,'source_quote',content)
    except ProviderError as exc:issues.append(str(exc))
    if not issues:issues.extend(check_board(chunk,text,external_ids=earlier_ids,allow_opening_continuation=True))
    if preproduction and not issues:
        from .preproduction import plan_board_asset_packing,validate_board
        try:validate_board(chunk,preproduction,plan_board_asset_packing(chunk,preproduction))
        except HTTPException as exc:issues.append(str(exc.detail))
    return issues


def segment_board(payload,plan,index,segment,passages,task_id,save_stage,model_preproduction,previous_chunk,treatment):
    """Plan one segment's shots from that segment's original text only."""
    starts=segments.shot_starts(plan);text=segments.segment_text(segment,passages)
    references=segments.references_of(segment)
    previous=None
    if previous_chunk is not None:
        before=plan.segments[index-1];last=previous_chunk.shots[-1]
        previous={'segment_id':before.id,'title':before.title,'beat':before.beat,
            'continuity_out':before.continuity_out,'last_shot_action':last.dramatic_action,
            'last_shot_continuity_out':last.continuity_out,'last_shot_meaning':last.viewer_knows}
    following=None
    if index+1<len(plan.segments):
        item=plan.segments[index+1]
        following={'segment_id':item.id,'title':item.title,'beat':item.beat,'purpose':item.purpose}
    attempts=[];repairs=[]
    earlier_ids=segments.earlier_shot_ids(plan,index)
    def chunk_contract(raw):
        errors=[];result=None
        try:result=Board.model_validate(raw)
        except ValidationError as exc:
            errors=board_validation_errors(exc,raw)
        if result is not None:
            result,changes=repair_board_causality(result);repairs.extend(changes)
        if result is not None:
            # Shots are numbered on the whole-film timeline so a cross-segment reference is never
            # confused with a local one. A wrong block is reported, never silently relabelled.
            expected=segments.shot_ids_for(plan,index,len(result.shots))
            actual=[shot.id for shot in result.shots]
            if actual!=expected:
                errors=[{'field':'shots.id','type':'shot_numbering','reason':
                    f'本片段的镜号必须接着上一片段连续编号：应为 {"、".join(expected)}，实际是 {"、".join(actual)}。'
                    f'调用方给出的 shot_start 是 S{segments.shot_starts(plan)[segment.id]:02}，请从它开始编号，不要每段都从 S01 重来。'}]
        if result is not None:
            invalid=[shot.id for shot in result.shots if shot.source_ref and shot.source_ref.upper() not in references]
            if invalid:
                errors=[{'field':'shots.source_ref','type':'segment_scope','reason':
                    '、'.join(invalid)+' 的 source_ref 必须来自本片段覆盖的原文编号：'+'、'.join(references)+
                    '。本片段以外的情节由其它片段负责，不要跨片段取材；不确定编号时留空并引用本片段原文。'}]
        if result is not None and not errors:
            try:repairs.extend(bind_sources(result.shots,passages,'source_quote',
                (payload.get('source') or {}).get('content','')))
            except ProviderError as exc:errors=[{'field':'shots.source_ref','reason':str(exc),'type':'source_binding'}]
        if result is not None and not errors:
            errors=[{'field':'board','reason':issue,'type':'structural_check'}
                    for issue in check_board(result,text,external_ids=earlier_ids,allow_opening_continuation=index>0)]
        if result is not None and not errors and model_preproduction:
            from .preproduction import plan_board_asset_packing,repair_board_assets,validate_board
            result,asset_repairs=repair_board_assets(result,model_preproduction);repairs.extend(asset_repairs)
            packing=plan_board_asset_packing(result,model_preproduction)
            try:validate_board(result,model_preproduction,packing)
            except HTTPException as exc:errors=[{'field':'assets','reason':str(exc.detail),'type':'asset_binding'}]
        if errors:
            attempts.append({'raw':raw,'errors':errors})
            save_stage('board_chunk_diagnostics',{'segment_id':segment.id,'references':list(references),
                'raw':raw,'errors':errors,'attempts':list(attempts)},36)
            details='；'.join(e['field']+'：'+e['reason'] for e in errors[:5])
            raise ModelOutputError(f'{segment.id}「{segment.title}」的镜头未按片段时间轴或素材约定输出：'+details)
        return result
    board,_=call_node(chat_json,'storyboard',{'source':{'title':plan.title,'content':text},
        'source_passages':{reference:passages[reference] for reference in references},
        'treatment':treatment,'brief':payload.get('brief',''),'preproduction':model_preproduction,
        'segment':{**segment.model_dump(),'index':index+1,'shot_start':starts[segment.id],
                   'shot_ids':segments.shot_ids_for(plan,index,segment.shot_budget),
                   'referenceable_shot_ids':earlier_ids},
        'segment_outline':segments.segment_outline(plan,index),'previous_segment':previous,'next_segment':following,
        'schema':chunk_schema(segment.shot_budget)},task_id,validator=chunk_contract)
    return board,repairs


def generate_chunks(payload,plan,cut,passages,task_id,save_stage,model_preproduction,treatment):
    """Plan one storyboard chunk per segment; already validated chunks are reused."""
    progress=payload.get('board_progress');reuse={};stamp=(model_preproduction or {}).get('stamp')
    # Reuse stays off for a whole-board retry, but a segment-scoped retry keeps every segment that
    # already passed validation so a retry does not repay for work that is still correct.
    allow_reuse=not payload.get('retry_feedback') or bool(payload.get('retry_reuse_chunks'))
    if (isinstance(progress,dict) and progress.get('fingerprint')==cut['fingerprint']
            and progress.get('preproduction_stamp')==stamp and allow_reuse):
        reuse={key:value for key,value in (progress.get('chunks') or {}).items() if isinstance(value,dict)}
    chunks=[];repairs=[];reused=[]
    saved_chunks={}
    for index,segment in enumerate(plan.segments):
        chunk=None
        if segment.id in reuse:
            try:candidate=Board.model_validate(reuse[segment.id])
            except ValidationError:candidate=None
            expected=segments.shot_ids_for(plan,index,len(candidate.shots)) if candidate is not None else None
            if candidate is not None and not chunk_issues(candidate,segment,passages,model_preproduction,
                    expected,segments.earlier_shot_ids(plan,index),payload.get('source',{}).get('content','')):
                chunk=candidate;reused.append(segment.id)
        if chunk is None:
            chunk,chunk_repairs=segment_board(payload,plan,index,segment,passages,task_id,save_stage,model_preproduction,
                chunks[-1] if chunks else None,treatment)
            repairs.extend(chunk_repairs)
        chunks.append(chunk)
        saved_chunks[segment.id]=chunk.model_dump()
        save_stage('board_progress',{'fingerprint':cut['fingerprint'],'segments':cut['segments'],
            'preproduction_stamp':stamp,'chunks':{**reuse,**saved_chunks},'reused':reused,'updated_at':time.time()},
            38+min(index+1,10))
    if reused:save_stage('board_chunk_reuse',{'segment_ids':reused},40)
    return chunks,repairs


def whole_board_contract(raw,passages,content,preproduction,save_stage):
    """Whole-film checks shared by the assembled board and the saved-board recovery path."""
    errors=[];repairs=[];packing={}
    try:result=Board.model_validate(raw)
    except ValidationError as exc:
        errors=board_validation_errors(exc,raw);result=None
    if result is not None:
        result,causality_repairs=repair_board_causality(result);repairs.extend(causality_repairs)
    if result is not None and preproduction:
        from .preproduction import plan_board_asset_packing,repair_board_assets,validate_board
        result,asset_repairs=repair_board_assets(result,preproduction);repairs.extend(asset_repairs)
        packing=plan_board_asset_packing(result,preproduction)
        try:validate_board(result,preproduction,packing)
        except HTTPException as exc:errors=[{'field':'assets','reason':str(exc.detail),'type':'asset_binding'}]
    if result is not None and not errors:
        try:repairs.extend(bind_sources(result.shots,passages,'source_quote',content))
        except ProviderError as exc:errors=[{'field':'shots.source_ref','reason':str(exc),'type':'source_binding'}]
    if result is not None and not errors:
        errors=[{'field':'board','reason':issue,'type':'structural_check'} for issue in check_board(result,content)]
    if errors:
        save_stage('board_diagnostics',{'raw':raw,'errors':errors},35)
        details='；'.join(e['field']+'：'+e['reason'] for e in errors[:5])
        raise ModelOutputError('分镜未按结构、原文或素材绑定约定输出：'+details)
    return result,repairs,packing


def compile_shot_prompts(board,preproduction,task_id,save_stage):
    """Compile镜头提示词 one segment window at a time instead of one merged call."""
    save_stage('board_plan',board.model_dump(),66)
    from .preproduction import shot_prompt_preproduction
    prompt_preproduction=shot_prompt_preproduction(preproduction) if preproduction else None
    for window in segments.shot_windows(board.model_dump()['shots']):
        chunk=Board(title=board.title,scope_note=('本窗口镜头：'+ '、'.join(shot['id'] for shot in window))[:600],
            shots=[Shot.model_validate(shot) for shot in window])
        attempts=[]
        def prompt_contract(compiled):
            errors=[]
            try:prompts=ShotPromptBatch.model_validate(compiled)
            except ValidationError as exc:
                errors=[{'field':'.'.join(str(x) for x in e['loc']),'reason':e['msg'],'type':e['type']}
                        for e in exc.errors(include_input=False,include_url=False)]
                prompts=None
            if prompts is not None:
                mapping={shot.id:shot for shot in prompts.shots}
                if set(mapping)!={shot.id for shot in chunk.shots} or len(mapping)!=len(prompts.shots):
                    errors=[{'field':'shots','reason':'生成提示词的格式或镜号不符合本片段镜头计划','type':'shot_mapping'}]
            if errors:
                attempts.append({'raw':compiled,'errors':errors})
                save_stage('prompt_diagnostics',{'shot_ids':[shot.id for shot in chunk.shots],
                    'raw':compiled,'errors':errors,'attempts':list(attempts)},66)
                raise ModelOutputError('镜头提示词未按镜号与格式约定输出：'+'；'.join(e['reason'] for e in errors[:5]))
            return prompts
        prompts,_=call_node(chat_json,'shot_prompts',
            {'board':chunk.model_dump(),'preproduction':prompt_preproduction,'schema':ShotPromptBatch.model_json_schema()},
            task_id,validator=prompt_contract)
        if not isinstance(prompts,ShotPromptBatch):prompts=prompt_contract(prompts)
        try:mapping={shot.id:shot for shot in prompts.shots}
        except ValueError:raise ProviderError('分镜提示词编译未通过，镜头计划已保留。') from None
        for shot in board.shots:
            if shot.id in mapping:
                shot.reference_prompt=mapping[shot.id].reference_prompt;shot.motion_prompt=mapping[shot.id].motion_prompt


def run_director(payload,task_id,save_stage):
    source=payload['source'];content=source['content']
    passages=source_passages(content)
    if payload.get('stage')=='board':
        treatment=Treatment.model_validate(payload['treatment'])
    else:
        save_stage('phase','正在阅读原文，提炼脑洞规则与人物目标',10)
        treatment_attempts=[]
        def treatment_contract(raw):
            errors=[]
            try:result=Treatment.model_validate(raw)
            except ValidationError as exc:
                errors=[{'field':'.'.join(str(x) for x in e['loc']),'reason':e['msg'],'type':e['type']} for e in exc.errors(include_input=False,include_url=False)]
                result=None
            if result is not None:
                try:bind_sources(result.rules,passages,'quote')
                except ProviderError as exc:errors=[{'field':'rules.source_ref','reason':str(exc),'type':'source_binding'}]
            if result is not None and not errors:
                missing=[f'规则 {i+1}：{rule.quote[:100]}' for i,rule in enumerate(result.rules) if not quote_exists(rule.quote,content)]
                if missing:errors=[{'field':'rules.quote','reason':'原文依据无法定位：'+'；'.join(missing),'type':'source_quote'}]
            if errors:
                treatment_attempts.append({'raw':raw,'errors':errors})
                save_stage('treatment_diagnostics',{'raw':raw,'errors':errors,'attempts':list(treatment_attempts)},20)
                raise ModelOutputError('导演阐述未按原文与格式约定输出：'+'；'.join(e['reason'] for e in errors[:5]))
            return result
        treatment,_=call_node(chat_json,'story_treatment',
            {'source':source,'source_passages':passages,'brief':payload['brief'],'schema':Treatment.model_json_schema()},task_id,
            validator=treatment_contract)
        save_stage('treatment',treatment.model_dump(),30)
        # The cut happens here, before any artwork exists. Two reasons: the design stage then only
        # creates the characters and places the scenes actually need, and every later generation
        # step works inside one segment instead of the whole story.
        segment_stage(payload,passages,task_id,save_stage,treatment)
    if payload.get('stage')=='treatment':return {'stage':'awaiting_preproduction'}
    preproduction=payload.get('preproduction')
    model_preproduction=preproduction
    if preproduction:
        from .preproduction import storyboard_preproduction
        model_preproduction=storyboard_preproduction(preproduction)
        if payload.get('retry_feedback'):
            model_preproduction={**model_preproduction,'previous_node_error':payload['retry_feedback']}
    saved=payload.get('saved_board')
    if saved is not None:
        try:board,repairs,packing=whole_board_contract(saved,passages,content,preproduction,save_stage)
        except ModelOutputError as exc:raise ProviderError(str(exc)) from None
    else:
        plan,cut,_=segment_stage(payload,passages,task_id,save_stage,treatment)
        save_stage('phase','正在按片段设计镜头调度、信息揭示与声音衔接',36)
        chunks,repairs=generate_chunks(payload,plan,cut,passages,task_id,save_stage,model_preproduction,treatment.model_dump())
        try:merged_shots=segments.merge_chunks(plan,chunks)
        except segments.NumberingError as exc:raise ProviderError(str(exc)+'请重新运行当前节点。') from None
        assembled=Board(title=cut['title'],shots=merged_shots,
            scope_note=('按微小说切割出的 '+str(len(plan.segments))+' 个片段分块生成；'+cut['overall_arc'])[:600])
        try:board,extra_repairs,packing=whole_board_contract(assembled.model_dump(),passages,content,preproduction,save_stage)
        except ModelOutputError as exc:raise ProviderError(str(exc)) from None
        repairs.extend(extra_repairs)
    if packing:
        from .preproduction import materialize_board_asset_packing,repair_board_assets,validate_board,storyboard_preproduction
        if not payload.get('project_id'):
            raise ProviderError('该镜需要本地图像工具拼接身份与服装参考板，但分镜任务缺少项目标识，不能继续。')
        try:preproduction,_=materialize_board_asset_packing(payload['project_id'],task_id,preproduction['stamp'],packing)
        except HTTPException as exc:raise ProviderError(str(exc.detail)) from None
        board,packed_repairs=repair_board_assets(board,preproduction);repairs.extend(packed_repairs)
        try:validate_board(board,preproduction)
        except HTTPException as exc:raise ProviderError(str(exc.detail)) from None
        model_preproduction=storyboard_preproduction(preproduction)
    if repairs:save_stage('board_repairs',{'changes':repairs},35)
    save_stage('board',board.model_dump(),65)
    if payload.get('professional_prompts'):
        compile_shot_prompts(board,preproduction,task_id,save_stage)
        save_stage('board',board.model_dump(),70)
    save_stage('phase','正在独立核对原文依据、连续性与生成可行性',75)
    review,_=call_node(chat_json,'storyboard_review',
        {'source':source,'treatment':treatment.model_dump(),'board':board.model_dump(),'schema':Review.model_json_schema()},task_id,
        validator=Review.model_validate)
    if not isinstance(review,Review):review=Review.model_validate(review)
    return review.model_dump()

class Create(BaseModel):
    restart_of:str|None=None
    request_key:str|None=Field(default=None,max_length=160)
    source_id:str
    brief:str=Field(default='制作原文内一个完整短场景，突出脑洞规则与人物反应。',max_length=1500)
    confirm_paid:bool=False

@router.get('/{source_id}')
def projects(source_id:str):
    with Session() as db:
        rows=db.scalars(select(Record).where(Record.kind=='director').order_by(Record.created.desc()))
        return [{**record_dict(r),'task':task_dict(db.get(Task,r.data['task_id']))} for r in rows if r.data.get('source_id')==source_id]

@router.post('')
def create(body:Create):
    cfg=settings()
    if not body.confirm_paid:raise HTTPException(422,'请确认本次导演阐述的模型调用。')
    refusal=paid_gate(cfg,'llm')
    if refusal:raise HTTPException(422,refusal)
    with Session.begin() as db:
        source=db.get(Record,body.source_id)
        if not source or source.kind!='story_source':raise HTTPException(404,'请先保存故事到本地工作台。')
        if '脑洞' not in source.data.get('labels',[]):raise HTTPException(422,'本阶段只开发脑洞标签线路。')
        if body.request_key:
            existing=next((p for p in db.scalars(select(Record).where(Record.kind=='director')) if p.data.get('request_key')==body.request_key),None)
            if existing:
                if existing.data.get('source_id')!=body.source_id:raise HTTPException(409,'制作轮次与原文不匹配。')
                if existing.data.get('brief')!=body.brief:raise HTTPException(409,'同一制作轮次的输入已改变，请建立新的测试轮次。')
                return {'project_id':existing.id,'task':task_dict(db.get(Task,existing.data['task_id']))}
        active=list(db.scalars(select(Task).where(Task.kind=='director',Task.status.in_(['queued','running','waiting']))))
        if any(t.payload.get('source_id')==body.source_id for t in active):raise HTTPException(409,'该故事已有导演任务正在执行。')
        if body.restart_of:
            previous=db.get(Record,body.restart_of)
            if not previous or previous.kind!='director' or previous.data.get('source_id')!=body.source_id or not previous.data.get('archived'):raise HTTPException(409,'请先归档同一故事的旧方案，再发起重做。')
        pid=uid('director');tid=uid('direct')
        snapshot={k:source.data.get(k) for k in ['title','author_name','labels','content','content_hash','completeness']}
        db.add(Record(id=pid,kind='director',data={'restart_of':body.restart_of,'request_key':body.request_key,'source_id':source.id,'source_hash':source.data['content_hash'],'pipeline':VERSION,'requires_preproduction':True,'status':'generating','task_id':tid,'brief':body.brief}))
        task=Task(id=tid,kind='director',payload={'mode':'live','stage':'treatment','project_id':pid,'source_id':source.id,'source':snapshot,'brief':body.brief,'pipeline':VERSION,'professional_prompts':True})
        db.add(task);db.flush();return {'project_id':pid,'task':task_dict(task)}

class Edit(BaseModel):
    version:int
    board:Board

@router.post('/projects/{project_id}/edit')
def edit(project_id:str,body:Edit):
    with Session.begin() as db:
        row=db.get(Record,project_id)
        if not row or row.kind!='director':raise HTTPException(404,'导演方案不存在。')
        if row.data.get('archived'):raise HTTPException(409,'此方案已归档，请恢复后操作或新建重做。')
        if row.data.get('requires_preproduction') and row.data.get('board'):
            from .preproduction import ready
            if ready(db,project_id)['stamp']!=row.data.get('preproduction_stamp'):raise HTTPException(409,'选角或搭景已变更，请重新生成分镜。')
        if row.version!=body.version:raise HTTPException(409,'方案已更新，请重新打开。')
        task=db.get(Task,row.data['task_id'])
        if task.status in ('queued','running','waiting'):raise HTTPException(409,'请等待生成结束或停止任务后修改。')
        source=db.get(Record,row.data['source_id'])
        if row.data.get('requires_preproduction'):
            from .preproduction import validate_board
            validate_board(body.board,ready(db,project_id))
        issues=check_board(body.board,source.data['content'])
        if issues:raise HTTPException(422,'；'.join(issues))
        row.data={**row.data,'board':body.board.model_dump(),'status':'pending_review','review':{'approved':False,'issues':['方案经过人工修改，原模型预审结果已失效，请逐镜复核。']}}
        row.version+=1;return record_dict(row)

class Approve(BaseModel):
    version:int
    confirm:bool=False
    note:str=Field(min_length=5,max_length=1000)

@router.post('/projects/{project_id}/approve')
def approve(project_id:str,body:Approve):
    if not body.confirm:raise HTTPException(422,'请逐镜审核后确认。')
    with Session.begin() as db:
        row=db.get(Record,project_id)
        if not row or row.kind!='director':raise HTTPException(404,'导演方案不存在。')
        if row.data.get('archived'):raise HTTPException(409,'此方案已归档，请恢复后操作或新建重做。')
        if row.data.get('requires_preproduction') and row.data.get('board'):
            from .preproduction import ready
            if ready(db,project_id)['stamp']!=row.data.get('preproduction_stamp'):raise HTTPException(409,'选角或搭景已变更，请重新生成分镜。')
        if row.version!=body.version:raise HTTPException(409,'方案已更新。')
        task=db.get(Task,row.data['task_id'])
        if task.status in ('queued','running','waiting'):raise HTTPException(409,'方案还在生成。')
        if 'board' not in row.data:raise HTTPException(422,'没有有效分镜可审核。')
        source=db.get(Record,row.data['source_id'])
        if check_board(Board.model_validate(row.data['board']),source.data['content']):raise HTTPException(422,'分镜结构检查未通过。')
        row.data={**row.data,'status':'approved','approval_note':body.note};row.version+=1
        db.add(Record(id=uid('audit'),kind='audit',data={'target':row.id,'action':'director_approved','note':body.note,'version':row.version}))
        return record_dict(row)

class Archive(BaseModel):
    version:int
    archived:bool=True
    note:str=Field(default='',max_length=1000)

@router.post('/projects/{project_id}/archive')
def archive(project_id:str,body:Archive):
    with Session.begin() as db:
        row=db.get(Record,project_id)
        if not row or row.kind!='director':raise HTTPException(404,'导演方案不存在。')
        if row.version!=body.version:raise HTTPException(409,'方案已更新，请刷新后归档。')
        if not body.archived and row.data.get('superseded_by_run'):raise HTTPException(409,'此轮制作已因上游重测失效，只可查看历史，请在当前轮次继续。')
        active=[t for t in db.scalars(select(Task).where(Task.status.in_(['queued','running','waiting']))) if t.payload.get('project_id')==project_id or t.payload.get('director_id')==project_id or t.payload.get('preproduction_id')==project_id or t.payload.get('creative_id')==project_id]
        if active:raise HTTPException(409,'该方案仍有执行中的任务，请在工作流记录停止或等待完成后归档。已提交供应商的调用可能仍收费。')
        row.data={**row.data,'archived':body.archived,'archive_note':body.note,'archived_at':__import__('time').time() if body.archived else None}
        row.version+=1
        db.add(Record(id=uid('audit'),kind='audit',data={'target':project_id,'action':'archived' if body.archived else 'restored','note':body.note}))
        return record_dict(row)

@router.get('/projects/{project_id}/history')
def project_history(project_id:str):
    with Session() as db:
        row=db.get(Record,project_id)
        if not row or row.kind!='director':raise HTTPException(404,'方案不存在。')
        jobs=[t for t in db.scalars(select(Task).order_by(Task.created.desc())) if t.payload.get('director_id')==project_id or t.payload.get('project_id')==project_id or t.payload.get('preproduction_id')==project_id or t.payload.get('creative_id')==project_id]
        ids={t.result.get(k) for t in jobs for k in ('asset_id','clip_id') if t.result.get(k)}
        return {'tasks':[task_dict(t) for t in jobs],'media':[record_dict(a) for aid in ids if (a:=db.get(Record,aid))]}


class BoardStart(BaseModel):
    version:int
    confirm_paid:bool=False
    retry_feedback:str|None=Field(default=None,max_length=1200)
    reuse_saved_chunks:bool=False
@router.post('/projects/{project_id}/storyboard')
def start_storyboard(project_id:str,body:BoardStart):
    cfg=settings()
    if not body.confirm_paid:raise HTTPException(422,'请确认分镜与预审调用。')
    refusal=paid_gate(cfg,'llm')
    if refusal:raise HTTPException(422,refusal)
    from .preproduction import ready
    with Session.begin() as db:
        row=db.get(Record,project_id)
        if not row or row.kind!='director' or row.data.get('archived'):raise HTTPException(409,'方案不存在或已归档。')
        if row.version!=body.version:raise HTTPException(409,'方案版本已变化。')
        if not row.data.get('treatment'):raise HTTPException(409,'先完成导演阐述。')
        if any(t.payload.get('project_id')==project_id or t.payload.get('director_id')==project_id or t.payload.get('preproduction_id')==project_id for t in db.scalars(select(Task).where(Task.status.in_(['queued','running','waiting'])))):raise HTTPException(409,'请等待当前任务结束。')
        prep=ready(db,project_id)
        original=db.get(Task,row.data['task_id'])
        source=original.payload['source']
        tid=uid('direct')
        t=Task(id=tid,kind='director',payload={'mode':'live','stage':'board','project_id':project_id,'source_id':row.data['source_id'],'source':source,'brief':row.data['brief'],'treatment':row.data['treatment'],'preproduction':prep,'professional_prompts':True,
            **({'retry_feedback':body.retry_feedback} if body.retry_feedback else {}),
            **({'retry_reuse_chunks':True} if body.reuse_saved_chunks else {}),
            **({'segments':row.data['segments']} if row.data.get('segments') else {}),
            **({'board_progress':row.data['board_progress']} if row.data.get('board_progress') else {})})
        # Retain old artifacts separately so a failed remake never destroys them.
        history=[*row.data.get('board_history',[])]
        previous={key:row.data.get(key) for key in ('board','review','board_diagnostics') if row.data.get(key) is not None}
        if previous:history.append({**previous,'version':row.version})
        history=history[-HISTORY_LIMIT:]
        row.data={k:v for k,v in row.data.items() if k not in ('board','review','board_diagnostics')}
        row.data={**row.data,'board_history':history,'requires_preproduction':True,'preproduction_stamp':prep['stamp'],'status':'generating','task_id':tid}
        row.version+=1;db.add(t);db.flush();return {'project_id':project_id,'task':task_dict(t)}
