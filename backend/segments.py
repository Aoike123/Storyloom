"""Cut the adopted micro-fiction into ordered segments before any shot is planned.

The cutting node reads the whole source once. Every later generation step reads a
single segment, so shot planning, prompt compilation and retries stay inside a
bounded window of the original text instead of one long merged model call.
"""
import hashlib
import json
import re
from typing import Literal
from pydantic import BaseModel,Field,ValidationError
from .providers import ModelOutputError,ProviderError

MAX_SEGMENTS=10
MAX_SHOTS_PER_SEGMENT=6
MAX_TOTAL_SHOTS=24
# Below this, a film has no room for setup, turn and payoff, so an unaffordable wallet is reported
# as insufficient instead of producing a fragment.
MIN_AFFORDABLE_SHOTS=4
SEGMENT_PATTERN=r'^G[0-9]{2}$'
REFERENCE_PATTERN=re.compile(r'^P([0-9]{3,})$')


class StorySegment(BaseModel):
    id:str=Field(pattern=SEGMENT_PATTERN,description='片段编号，从 G01 起按剧情顺序连续递增。')
    title:str=Field(min_length=1,max_length=80,description='片段标题，供创作者与后续节点识别。')
    source_refs:list[str]=Field(min_length=1,max_length=8,
        description='本片段覆盖的原文 P 编号。每个 P 编号只能属于一个片段：上一片段以 P007 结束时，本片段必须从 P008 开始，'
                    '不要把 P007 再列一次，也不要跳过 P008。片段内 P 编号升序、不重复。')
    beat:Literal['setup','development','turn','climax','resolution']=Field(
        description='本片段在整篇里的剧情功能；一个片段只承担一个功能。')
    purpose:str=Field(min_length=5,max_length=400,description='本片段必须交代的信息，以及留给下一片段的悬念。')
    characters:list[str]=Field(default_factory=list,max_length=8,description='本片段实际出场的已确认角色名称。')
    location:str=Field(min_length=1,max_length=120,description='本片段的主要物理场景，只能是原文已出现的地点。')
    shot_budget:int=Field(ge=1,le=MAX_SHOTS_PER_SEGMENT,description='本片段计划占用的镜头数；全部片段合计不超过 24 镜。')
    continuity_out:str=Field(min_length=1,max_length=300,
        description='本片段结尾的人物位置、状态与未解决的信息，供下一片段直接承接。')


class SegmentPlan(BaseModel):
    title:str=Field(min_length=1,max_length=120,description='本次改编的整体标题。')
    overall_arc:str=Field(min_length=1,max_length=800,description='把全部片段串成一条因果主线，说明每个片段为什么必须存在。')
    segments:list[StorySegment]=Field(min_length=1,max_length=MAX_SEGMENTS)


def reference_number(reference):
    match=REFERENCE_PATTERN.fullmatch(str(reference or '').strip().upper())
    return int(match.group(1)) if match else None


def references_of(segment):
    return list(segment['source_refs'] if isinstance(segment,dict) else segment.source_refs)


def plan_issues(plan,passages,max_total_shots=None):
    """Deterministic original-text coverage check: contiguous, ordered, non-overlapping."""
    limit=max_total_shots if max_total_shots is not None else MAX_TOTAL_SHOTS
    issues=[];previous=None;total=0
    actual=[segment.id for segment in plan.segments]
    expected=[f'G{index+1:02}' for index in range(len(actual))]
    if actual!=expected:
        issues.append('片段编号必须从 G01 起连续递增：'+ '、'.join(actual)+' 不符合。')
    repeated=[value for value in actual if actual.count(value)>1]
    if repeated:
        issues.append('片段编号重复：'+ '、'.join(sorted(set(repeated)))+'。')
    for segment in plan.segments:
        numbers=[]
        for reference in references_of(segment):
            number=reference_number(reference)
            if number is None or reference not in passages:
                issues.append(f'{segment.id} 引用了不存在的原文编号 {reference}；可用编号形如 '+ '、'.join(list(passages)[:3])+'。')
                continue
            numbers.append(number)
        if numbers!=sorted(numbers) or len(set(numbers))!=len(numbers):
            issues.append(f'{segment.id} 的原文编号必须从小到大且不重复：'+ '、'.join(references_of(segment))+'。')
            numbers=sorted(set(numbers))
        if numbers and previous is not None and numbers[0]!=previous+1:
            start=numbers[0]
            if start<=previous:
                # The message has to say which way is wrong: an earlier wording ("必须连续：上一片段
                # 结束于 P006") was read by the model as "start again at P006", so every retry repeated
                # the same boundary passage.
                issues.append(f'{segment.id} 重复覆盖了上一片段已经用过的原文：{segment.id} 从 P{start:03} 开始，'
                              f'但上一片段已经覆盖到 P{previous:03}。每个 P 编号只能属于一个片段，'
                              f'{segment.id} 必须从 P{previous+1:03} 开始，并且不要列出 P{previous:03}。')
            else:
                missing='、'.join(f'P{number:03}' for number in range(previous+1,start))
                issues.append(f'{segment.id} 漏掉了原文：上一片段结束于 P{previous:03}，{segment.id} 却从 P{start:03} 开始，'
                              f'中间的 {missing} 没有归属。{segment.id} 必须从 P{previous+1:03} 开始。')
        if numbers:previous=numbers[-1]
        total+=segment.shot_budget
    if total>limit:
        issues.append(f'全部片段合计 {total} 镜，超过当前上限 {limit} 镜；请合并次要片段或下调 shot_budget。')
    return issues


def validate_plan_output(raw,passages,max_total_shots=None):
    """Segment-plan validator used by ``call_node`` so retries repeat the same rules."""
    try:
        plan=SegmentPlan.model_validate(raw)
    except ValidationError as exc:
        details='；'.join('.'.join(str(x) for x in error['loc'])+'：'+error['msg']
            for error in exc.errors(include_input=False,include_url=False)[:5])
        raise ModelOutputError('微小说切割未按片段结构输出：'+details) from None
    issues=plan_issues(plan,passages,max_total_shots)
    if issues:raise ModelOutputError('微小说切割未通过原文覆盖检查：'+'；'.join(issues[:5]))
    return plan


def plan_from_saved(saved):
    try:
        return SegmentPlan.model_validate(saved)
    except (ValidationError,TypeError):
        raise ProviderError('已保存的微小说片段不完整，不能继续分镜；请重新运行当前节点。') from None


def segment_text(segment,passages):
    return ''.join(passages.get(reference,'') for reference in references_of(segment))


def segment_outline(plan,index):
    """Every other segment's role, without原文，so one chunk still sees the whole arc."""
    return [{'id':segment.id,'title':segment.title,'beat':segment.beat,'purpose':segment.purpose,
             'source_refs':list(segment.source_refs),'shot_budget':segment.shot_budget}
            for position,segment in enumerate(plan.segments) if position!=index]


def unit_id(segment):
    return segment if isinstance(segment,str) else segment.id


def shot_ids_for(segment,count):
    """The shot ids one episode's shots must carry: local numbering with the episode prefix.

    Numbering restarts inside every episode, so an episode can be planned, retried and published on
    its own while the prefix keeps every id unique in the merged film.
    """
    gid=unit_id(segment)
    return [f'{gid}-S{offset+1:02}' for offset in range(count)]


def earlier_shot_ids(boards):
    """Ids that already exist: every shot of the episodes produced before this one."""
    return [shot.id for board in boards for shot in board.shots]


def plan_fingerprint(plan,passages):
    payload={'title':plan.title,'overall_arc':plan.overall_arc,
             'segments':[{**segment.model_dump(),'source_text':segment_text(segment,passages)} for segment in plan.segments]}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:32]


def plan_dump(plan,passages):
    """Serializable plan carrying each segment's exact原文 for display and reuse."""
    data=plan.model_dump()
    for segment in data['segments']:
        segment['source_text']=segment_text(segment,passages)
    data['shot_total']=sum(segment.shot_budget for segment in plan.segments)
    data['fingerprint']=plan_fingerprint(plan,passages)
    return data


def segment_schema():
    return SegmentPlan.model_json_schema()


def shot_windows(shots,limit=MAX_SHOTS_PER_SEGMENT):
    """Group merged shots for prompt compilation; one storyboard chunk stays one window."""
    groups=[]
    for shot in shots:
        tag=str(shot.get('segment_id') or '')
        if groups and len(groups[-1][1])<limit and (not tag or not groups[-1][0] or tag==groups[-1][0]):
            groups[-1][1].append(shot);continue
        groups.append((tag,[shot]))
    return [window for _,window in groups]


def merge_chunks(plan,chunks):
    """Join one finished episode board per episode into the merged film, in episode order.

    Ids already carry their episode prefix, so merging never renumbers a shot; the only thing that
    is written here is the continuity pointer from an episode's first shot to the previous one.
    """
    merged=[]
    for index,(segment,chunk) in enumerate(zip(plan.segments,chunks)):
        for position,shot in enumerate(chunk.shots):
            item=shot.model_copy(deep=True)
            item.segment_id=segment.id
            if position==0 and merged:
                previous=merged[-1].id
                prefix=f'承接 {previous}（上一片段结尾）'
                note=item.continuity_in.strip()
                if previous not in item.setup_ids and item.purpose in ('reveal','payoff'):
                    item.setup_ids=[*item.setup_ids,previous]
                item.continuity_in=((prefix+'：'+note) if note else prefix)[:400]
            merged.append(item)
    return merged
