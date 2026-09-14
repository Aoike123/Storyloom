"""Brainstorm-fiction directing: grounded treatment, shot design, independent review."""
from typing import Literal
import re
import unicodedata
from pydantic import AliasChoices,BaseModel,Field,ValidationError
from fastapi import APIRouter,HTTPException
from sqlalchemy import select
from .db import Record,Task,Session,uid,record_dict,task_dict
from .providers import settings,chat_json,ProviderError
from .skill_runtime import call_node,render_node

VERSION='brainstorm-1.0'
router=APIRouter(prefix='/api/director',tags=['director'])

class Rule(BaseModel):
    source_ref:str=Field(default='',max_length=20)
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
    scene:str=Field(min_length=1,max_length=160)
    purpose:Literal['hook','setup','rule','escalation','reveal','reaction','payoff','bridge']
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
    setup_ids:list[str]=Field(default_factory=list,max_length=8)
    edit_seconds:float=Field(ge=1,le=15)
    generation_seconds:int=Field(ge=5,le=15)
    dialogue:str=Field(max_length=400)
    sound:str=Field(min_length=1,max_length=400)
    transition:str=Field(min_length=1,max_length=300)
    reference_prompt:str=Field(min_length=5,max_length=1500,validation_alias=AliasChoices('reference_prompt','first_frame'),description='镜头参考图设计，用于构图、人物定装与场景外观参考，不限定视频起止帧')
    motion_prompt:str=Field(min_length=5,max_length=1500)
    assets:list[str]=Field(min_length=1,max_length=12)
    generation_risk:str=Field(min_length=1,max_length=400)

class Board(BaseModel):
    title:str=Field(min_length=1,max_length=120)
    scope_note:str=Field(min_length=1,max_length=600)
    shots:list[Shot]=Field(min_length=4,max_length=8)

class Review(BaseModel):
    approved:bool
    issues:list[str]=Field(default_factory=list,max_length=20)
    continuity:str
    dramatic_logic:str
    editability:str
    production_feasibility:str


class ShotPrompt(BaseModel):
    id:str=Field(pattern=r'^S[0-9]{2}$')
    reference_prompt:str=Field(min_length=5,max_length=1500,validation_alias=AliasChoices('reference_prompt','first_frame'),description='静态镜头参考图提示词，不指定视频首尾帧')
    motion_prompt:str=Field(min_length=5,max_length=1500)


class ShotPromptBatch(BaseModel):
    shots:list[ShotPrompt]=Field(min_length=4,max_length=8)

def normalized_quote(text):
    # Typography-only tolerance; words, negations and sentence order remain intact.
    return ''.join(c for c in unicodedata.normalize('NFKC',text).translate(str.maketrans({'“':'"','”':'"','「':'"','」':'"','‘':"'",'’':"'"})) if not c.isspace())

def quote_exists(quote,content):
    return bool(normalized_quote(quote)) and normalized_quote(quote) in normalized_quote(content)

def source_passages(content):
    return {f'P{i+1:03}':content[start:start+240] for i,start in enumerate(range(0,len(content),240))}

def bind_sources(items,passages,field):
    for item in items:
        if item.source_ref:
            if item.source_ref not in passages:raise ProviderError(f'原文依据编号 {item.source_ref} 不存在，已保存草稿。')
            setattr(item,field,passages[item.source_ref])

def check_board(board,content):
    issues=[];seen=set()
    for shot in board.shots:
        if shot.id in seen:issues.append(f'{shot.id} 镜号重复')
        if not quote_exists(shot.source_quote,content):issues.append(f'{shot.id} 原文依据无法定位')
        if any(x not in seen for x in shot.setup_ids):issues.append(f'{shot.id} 铺垫必须引用前序镜头')
        if shot.purpose in ('reveal','payoff') and not shot.setup_ids:issues.append(f'{shot.id} 揭示或回收缺少前序铺垫')
        if shot.edit_seconds>shot.generation_seconds:issues.append(f'{shot.id} 剪辑时长超过生成素材时长')
        seen.add(shot.id)
    return issues

def run_director(payload,task_id,save_stage):
    source=payload['source'];content=source['content']
    passages=source_passages(content)
    if payload.get('stage')=='board':
        treatment=Treatment.model_validate(payload['treatment'])
    else:
        save_stage('phase','正在阅读原文，提炼脑洞规则与人物目标',10)
        treatment,_=call_node(chat_json,'story_treatment',
            {'source':source,'source_passages':passages,'brief':payload['brief'],'schema':Treatment.model_json_schema()},task_id)
        try:treatment=Treatment.model_validate(treatment)
        except ValueError:raise ProviderError('导演阐述格式未通过，请检查任务记录后新建。') from None
        save_stage('treatment',treatment.model_dump(),20)
        bind_sources(treatment.rules,passages,'quote')
        missing=[f'规则 {i+1}：{rule.quote[:100]}' for i,rule in enumerate(treatment.rules) if not quote_exists(rule.quote,content)]
        if missing:raise ProviderError('原文依据无法定位，已保留导演草稿；'+ '；'.join(missing))
        save_stage('treatment',treatment.model_dump(),30)
    if payload.get('stage')=='treatment':return {'stage':'awaiting_preproduction'}
    save_stage('phase','正在设计镜头调度、信息揭示与声音衔接',35)
    raw=payload.get('saved_board')
    if raw is None:
        raw,_=call_node(chat_json,'storyboard',
            {'source':source,'source_passages':passages,'treatment':treatment.model_dump(),'brief':payload['brief'],'preproduction':payload.get('preproduction'),'schema':Board.model_json_schema()},task_id)
    try:board=Board.model_validate(raw)
    except ValidationError as exc:
        errors=[{'field':'.'.join(str(x) for x in e['loc']),'reason':e['msg'],'type':e['type']} for e in exc.errors(include_input=False,include_url=False)]
        save_stage('board_diagnostics',{'raw':raw,'errors':errors},35)
        details='；'.join(e['field']+'：'+e['reason'] for e in errors[:5])
        raise ProviderError('分镜格式校验失败（非剧情审核）；原始输出已保留。'+details) from None
    if payload.get('preproduction'):
        from .preproduction import repair_board_assets,validate_board
        board,repairs=repair_board_assets(board,payload['preproduction'])
        if repairs:save_stage('board_repairs',{'changes':repairs},35)
        try:validate_board(board,payload['preproduction'])
        except HTTPException as e:
            save_stage('board_diagnostics',{'raw':raw,'errors':[{'field':'assets','reason':e.detail}]},35)
            raise ProviderError(e.detail) from None
    save_stage('board',board.model_dump(),60)
    bind_sources(board.shots,passages,'source_quote')
    issues=check_board(board,content)
    save_stage('board',board.model_dump(),65)
    if issues:return {'approved':False,'issues':issues,'stage':'structural_check'}
    if payload.get('professional_prompts'):
        save_stage('board_plan',board.model_dump(),66)
        compiled,_=call_node(chat_json,'shot_prompts',{'board':board.model_dump(),'preproduction':payload.get('preproduction'),'schema':ShotPromptBatch.model_json_schema()},task_id)
        try:
            prompts=ShotPromptBatch.model_validate(compiled)
            mapping={shot.id:shot for shot in prompts.shots}
            if set(mapping)!={shot.id for shot in board.shots} or len(mapping)!=len(prompts.shots):raise ValueError('镜号缺失或重复')
        except ValueError:
            save_stage('prompt_diagnostics',{'raw':compiled,'errors':[{'field':'shots','reason':'生成提示词的格式或镜号不符合分镜计划'}]},66)
            raise ProviderError('分镜提示词编译未通过，镜头计划已保留。') from None
        for shot in board.shots:
            shot.reference_prompt=mapping[shot.id].reference_prompt;shot.motion_prompt=mapping[shot.id].motion_prompt
        save_stage('board',board.model_dump(),70)
    save_stage('phase','正在独立核对原文依据、连续性与生成可行性',75)
    review,_=call_node(chat_json,'storyboard_review',
        {'source':source,'treatment':treatment.model_dump(),'board':board.model_dump(),'schema':Review.model_json_schema()},task_id)
    try:return Review.model_validate(review).model_dump()
    except ValueError:raise ProviderError('独立检查输出无效，分镜保留待人工处理。') from None

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
    if not body.confirm_paid or not cfg['paid_enabled'] or not cfg['llm_configured']:raise HTTPException(422,'请配置语言模型并确认本次导演阐述收费调用。')
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

class Frame(BaseModel):
    remake:bool=False
    version:int
    confirm_paid:bool=False

@router.post('/projects/{project_id}/shots/{shot_id}/image')
def frame(project_id:str,shot_id:str,body:Frame):
    cfg=settings()
    if not body.confirm_paid or not cfg['paid_enabled'] or not cfg['image_configured']:raise HTTPException(422,'请配置生图并确认收费。')
    with Session.begin() as db:
        row=db.get(Record,project_id)
        if not row or row.kind!='director':raise HTTPException(404,'导演方案不存在。')
        if row.data.get('archived'):raise HTTPException(409,'此方案已归档。')
        if row.version!=body.version or row.data['status']!='approved':raise HTTPException(409,'请先审核当前版本分镜。')
        shot=next((s for s in row.data['board']['shots'] if s['id']==shot_id),None)
        if not shot:raise HTTPException(404,'镜头不存在。')
        from .consistency import image_context
        context=image_context(db,project_id,shot_id)
        existing=next((t for t in db.scalars(select(Task).where(Task.kind=='image').order_by(Task.created.desc())) if t.payload.get('director_id')==project_id and t.payload.get('director_version')==row.version and t.payload.get('shot_id')==shot_id and t.payload.get('consistency_stamp')==context['consistency_stamp'] and t.status in ('queued','running','waiting','completed')),None)
        if existing and (not body.remake or existing.status!='completed'):return task_dict(existing)
        task=Task(id=uid('image'),kind='image',payload={**context,'mode':'live',**render_node('frame_render',{'style':context['visual_style'],'references':str(context['reference_assets']),'continuity':context['continuity_state'],'reference_prompt':shot.get('reference_prompt') or shot.get('first_frame')}),'title':row.data['board']['title']+' '+shot_id,
            'director_id':row.id,'director_version':row.version,'shot_id':shot_id,'motion_prompt':shot['motion_prompt'],
            'generation_seconds':shot['generation_seconds'],'edit_seconds':shot['edit_seconds'],
            **({'revision_of':existing.id} if existing else {})})
        db.add(task);db.flush();return task_dict(task)


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
@router.post('/projects/{project_id}/storyboard')
def start_storyboard(project_id:str,body:BoardStart):
    cfg=settings()
    if not body.confirm_paid or not cfg['paid_enabled'] or not cfg['llm_configured']:raise HTTPException(422,'请配置文本模型并确认分镜与预审调用。')
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
        t=Task(id=tid,kind='director',payload={'mode':'live','stage':'board','project_id':project_id,'source_id':row.data['source_id'],'source':source,'brief':row.data['brief'],'treatment':row.data['treatment'],'preproduction':prep,'professional_prompts':True})
        # Retain old artifacts separately so a failed remake never destroys them.
        history=[*row.data.get('board_history',[])]
        if row.data.get('board'):history.append({'board':row.data['board'],'review':row.data.get('review'),'version':row.version})
        row.data={k:v for k,v in row.data.items() if k not in ('board','review','board_diagnostics')}
        row.data={**row.data,'board_history':history,'requires_preproduction':True,'preproduction_stamp':prep['stamp'],'status':'generating','task_id':tid}
        row.version+=1;db.add(t);db.flush();return {'project_id':project_id,'task':task_dict(t)}
