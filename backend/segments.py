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
        description='本片段覆盖的原文 P 编号，必须从小到大连续排列，不与前后片段重叠或跳段。')
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
            issues.append(f'{segment.id} 与上一片段之间必须连续：上一片段结束于 P{previous:03}，本片段从 P{numbers[0]:03} 开始。'
                          '相邻片段不得跳段，也不得重复覆盖同一段原文。')
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


def shot_starts(plan):
    """Return the 1-based全片起始镜号 of every segment, in plan order."""
    starts={};number=1
    for segment in plan.segments:
        starts[segment.id]=number;number+=segment.shot_budget
    return starts


def shot_ids_for(plan,index,count):
    """The exact whole-film shot ids one segment's shots must carry.

    Every chunk is numbered on the whole-film timeline, so a cross-segment reference can never be
    mistaken for a local one and the caller never has to relabel a shot silently.
    """
    start=shot_starts(plan)[plan.segments[index].id]
    return [f'S{start+offset:02}' for offset in range(count)]


def earlier_shot_ids(plan,index):
    """Whole-film ids that exist before this segment and may be referenced as setup."""
    start=shot_starts(plan)[plan.segments[index].id]
    return [f'S{number:02}' for number in range(1,start)]


class NumberingError(ValueError):
    """A chunk used shot numbers that do not match the whole-film timeline."""


def plan_fingerprint(plan,passages):
    payload={'title':plan.title,'overall_arc':plan.overall_arc,
             'segments':[{**segment.model_dump(),'source_text':segment_text(segment,passages)} for segment in plan.segments]}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:32]


def plan_dump(plan,passages):
    """Serializable plan carrying each segment's exact原文 for display and reuse."""
    starts=shot_starts(plan);data=plan.model_dump()
    for segment in data['segments']:
        segment['source_text']=segment_text(segment,passages)
        segment['shot_start']=starts[segment['id']]
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
    """Join one storyboard chunk per segment into a single continuous shot list.

    Chunks are planned on the whole-film timeline, so merging only labels each shot with its
    segment and verifies the numbering. A wrong number is reported instead of being relabelled,
    because relabelling is what silently turned "continues the opening" into "continues this
    chunk's own first shot".
    """
    merged=[]
    for index,(segment,chunk) in enumerate(zip(plan.segments,chunks)):
        expected=shot_ids_for(plan,index,len(chunk.shots))
        actual=[shot.id for shot in chunk.shots]
        if actual!=expected:
            raise NumberingError(f'{segment.id} 的镜号必须按整片时间轴连续编号：应为 {"、".join(expected)}，'
                                 f'实际是 {"、".join(actual)}。请改用调用方给出的 shot_start 起算的镜号，不要每段都从 S01 重新开始。')
        for position,shot in enumerate(chunk.shots):
            item=shot.model_copy(deep=True)
            item.segment_id=segment.id
            if position==0 and index>0:
                previous=shot_ids_for(plan,index-1,len(chunks[index-1].shots))[-1]
                prefix=f'承接 {previous}（上一片段结尾）'
                note=item.continuity_in.strip()
                if previous not in item.setup_ids and item.purpose in ('reveal','payoff'):
                    item.setup_ids=[*item.setup_ids,previous]
                item.continuity_in=((prefix+'：'+note) if note else prefix)[:400]
            merged.append(item)
    return merged
