"""Fast, versioned reader branches built only from locked visual references."""
import copy
import math
import re
import time
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from .db import DATA, Record, Session, Task, uid
from .environment import model_config
from .providers import chat_json, paid_gate, settings
from .skill_runtime import call_node, render_node
from .video_files import extract_frame_at_second, media_path
from .video_storage import file_info, snapshot_asset, verify_file


router = APIRouter(prefix='/api/reader', tags=['reader-branch'])
SESSION_PATTERN = re.compile(r'^[A-Za-z0-9_-]{16,80}$')
FORBIDDEN_VISUAL_CHANGE = re.compile(
    r'新场景|另一个场景|切换场景|转场到|来到(?:新的|另一个)|换装|换上|改穿|'
    r'脱下.{0,8}(?:衣|外套)|新角色|新增人物|陌生人出现|又一个人'
)


class BranchShot(BaseModel):
    id: str = Field(pattern=r'^B[0-9]{2}$')
    purpose: Literal['consequence', 'reaction', 'resolution', 'bridge']
    action: str = Field(min_length=2, max_length=600)
    dialogue: str = Field(default='', max_length=300)
    causal_result: str = Field(min_length=2, max_length=500)
    continuity_in: str = Field(min_length=2, max_length=500)
    continuity_out: str = Field(min_length=2, max_length=500)
    generation_seconds: int = Field(default=5, ge=5, le=8)
    uses_new_scene: Literal[False] = False
    uses_new_character: Literal[False] = False
    changes_costume: Literal[False] = False


class BranchPlan(BaseModel):
    accepted: bool = True
    conflict: str = Field(default='', max_length=600)
    summary: str = Field(min_length=2, max_length=600)
    causal_chain: list[str] = Field(default_factory=list, max_length=6)
    terminal: bool = False
    rejoin_index: int | None = Field(default=None, ge=0)
    shots: list[BranchShot] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def coherent_shape(self):
        if self.accepted:
            if not self.shots or not self.causal_chain:
                raise ValueError('可执行分支必须包含因果链和至少一个镜头。')
            if self.terminal != (self.rejoin_index is None):
                raise ValueError('终止方式与回归位置不一致。')
        elif self.shots or not self.conflict:
            raise ValueError('不可执行分支必须说明冲突，且不能派发镜头。')
        return self


class BranchCommand(BaseModel):
    release_id: str = Field(min_length=1, max_length=80)
    base_branch_id: str | None = Field(default=None, max_length=80)
    index: int = Field(ge=0)
    offset: float = Field(ge=0, allow_inf_nan=False)
    text: str = Field(min_length=2, max_length=2000)
    confirm_generation: bool = False


def _session(value):
    if not isinstance(value, str) or not SESSION_PATTERN.fullmatch(value):
        raise HTTPException(422, '读者会话无效，请刷新页面后重试。')
    return value


def _release(db, release_id):
    row = db.get(Record, release_id)
    if not row or row.kind != 'reader_release':
        raise HTTPException(404, '作品不存在。')
    if row.data.get('superseded_by_run'):
        raise HTTPException(409, '此作品版本已失效，请刷新目录。')
    return row


def _task_entry(db, branch, shot, task_id, order):
    task = db.get(Task, task_id)
    common = {
        'occurrence_id': f'{branch.id}:{shot["id"]}', 'kind': 'branch',
        'branch_shot_id': shot['id'], 'shot_id': shot['id'], 'label': '分支 ' + shot['id'],
        'order': order, 'summary': shot['action'],
    }
    if not task:
        return {**common, 'status': 'failed', 'message': '分支视频任务记录缺失。'}
    if task.status == 'completed':
        clip = db.get(Record, task.result.get('clip_id', ''))
        if clip and clip.kind == 'clip' and clip.data.get('media') and clip.data.get('duration', 0) > 0:
            return {**common, 'status': 'ready', 'clip_id': clip.id, 'media': clip.data['media'],
                    'start': 0, 'end': clip.data['duration'], 'task_id': task.id}
    status = 'failed' if task.status in ('failed', 'needs_review', 'cancelled', 'superseded') else 'pending'
    return {**common, 'status': status, 'task_id': task.id, 'message': task.message}


def _manifest(db, branch):
    plan = branch.data.get('plan')
    if not isinstance(plan, dict) or not plan.get('accepted'):
        return []
    task_ids = {item['shot_id']: item['task_id'] for item in branch.data.get('video_tasks', [])}
    entries = [_task_entry(db, branch, shot, task_ids.get(shot['id'], ''), index)
               for index, shot in enumerate(plan.get('shots', []))]
    if plan.get('rejoin_index') is not None:
        release = _release(db, branch.data['release_id'])
        for original_index in range(plan['rejoin_index'], len(release.data.get('entries', []))):
            original = copy.deepcopy(release.data['entries'][original_index])
            entries.append({**original,
                'occurrence_id': f'{branch.id}:rejoin:{original_index}', 'kind': 'original',
                'status': 'ready', 'label': '原版衔接', 'original_index': original_index})
    return entries


def _public_branch(db, branch):
    plan_task = db.get(Task, branch.data.get('plan_task_id', ''))
    plan = branch.data.get('plan')
    entries = _manifest(db, branch)
    generated = [entry for entry in entries if entry.get('kind') == 'branch']
    ready_count = sum(entry.get('status') == 'ready' for entry in generated)
    failed = next((entry for entry in generated if entry.get('status') == 'failed'), None)
    if branch.data.get('status') == 'rejected':
        status, message = 'rejected', branch.data.get('message') or '这次改写超出了当前可复用的场景与人物范围。'
    elif plan is None and plan_task and plan_task.status in ('failed', 'needs_review', 'cancelled'):
        status, message = 'failed', plan_task.message
    elif plan is None:
        status, message = 'planning', plan_task.message if plan_task else '正在梳理剧情因果。'
    elif failed:
        status, message = 'failed', failed.get('message') or '分支片段没有完成，请在当前画面重新尝试。'
    elif generated and ready_count == len(generated):
        status, message = 'ready', ('分支已经完整就绪，将在因果闭环处收束。' if plan.get('terminal')
                                    else '分支已经完整就绪，并会在合理位置接回原故事。')
    elif ready_count:
        status, message = 'generating', '第一段分支已经就绪，后续片段仍在后台生成。'
    else:
        status, message = 'generating', '因果规划已完成，正在生成第一段分支视频。'
    if branch.data.get('superseded_by') and status not in ('rejected', 'failed'):
        status, message = 'superseded', '这条分支已由更新的读者选择接替。'
    return {
        'id': branch.id, 'version': branch.version, 'release_id': branch.data['release_id'],
        'base_branch_id': branch.data.get('base_branch_id'), 'branch_version': branch.data['branch_version'],
        'status': status, 'message': message, 'lock_forward_seek': status != 'rejected',
        'intent': branch.data['intent'], 'cut': copy.deepcopy(branch.data['cut']),
        'summary': plan.get('summary') if isinstance(plan, dict) else None,
        'terminal': plan.get('terminal') if isinstance(plan, dict) else None,
        'rejoin_index': plan.get('rejoin_index') if isinstance(plan, dict) else None,
        'entries': entries, 'ready_count': ready_count, 'generated_count': len(generated),
    }


def _parent_history(db, parent):
    history, seen, current = [], set(), parent
    while current and current.id not in seen and len(history) < 6:
        seen.add(current.id)
        history.append({'intent': current.data.get('intent'), 'result': (current.data.get('plan') or {}).get('summary')})
        parent_id = current.data.get('base_branch_id')
        current = db.get(Record, parent_id) if parent_id else None
    return list(reversed(history))


def _resolve_cut(db, body, session_id, release):
    if not body.base_branch_id:
        entries = release.data.get('entries', [])
        if body.index >= len(entries):
            raise HTTPException(422, '暂停片段不存在。')
        entry = copy.deepcopy(entries[body.index])
        return entry, body.index, body.index == len(entries) - 1, [], None, body.index + 1
    parent = db.get(Record, body.base_branch_id)
    if not parent or parent.kind != 'reader_branch' or parent.data.get('reader_session_id') != session_id:
        raise HTTPException(404, '读者分支不存在。')
    if parent.data.get('release_id') != release.id:
        raise HTTPException(409, '分支与作品版本不一致。')
    manifest = _manifest(db, parent)
    if body.index >= len(manifest) or manifest[body.index].get('status') != 'ready':
        raise HTTPException(422, '只能在已经播放就绪的片段中继续改写。')
    entry = copy.deepcopy(manifest[body.index])
    if entry.get('kind') == 'original':
        original_index = entry['original_index']
        at_end = original_index == len(release.data.get('entries', [])) - 1
        min_rejoin_index = original_index + 1
    else:
        original_index = parent.data['cut']['original_index']
        at_end = bool((parent.data.get('plan') or {}).get('terminal'))
        min_rejoin_index = (parent.data.get('plan') or {}).get('rejoin_index') or original_index + 1
    return entry, original_index, at_end, _parent_history(db, parent), parent, min_rejoin_index


def _visual_context(db, release, source_entry, original_index, cut_file):
    bindings = [{'role': 'cut_frame', 'name': '读者实际暂停画面', 'file': cut_file}]
    locked = []
    director = db.get(Record, release.data.get('director_id', ''))
    shot_id = source_entry.get('shot_id')
    original_entry = source_entry
    if shot_id and shot_id.startswith('B') and director:
        originals = release.data.get('entries', [])
        if original_index < len(originals):
            original_entry = originals[original_index]
            shot_id = original_entry.get('shot_id')
    source_clip = db.get(Record, original_entry.get('clip_id', ''))
    frozen_bindings = ((source_clip.data.get('input_snapshot') or {}).get('asset_bindings', [])
                       if source_clip and source_clip.kind == 'clip' else [])
    for frozen in frozen_bindings:
        if len(bindings) >= 9 or not isinstance(frozen, dict) or not frozen.get('file'):
            continue
        verify_file(frozen['file'])
        bindings.append({'role': 'locked_reference', **copy.deepcopy(frozen)})
        locked.append({'asset_id': frozen.get('asset_id'), 'role': frozen.get('type') or frozen.get('role'),
                       'name': frozen.get('name'), 'notes': (frozen.get('description') or '')[:500]})
    visual = db.get(Record, 'visual_' + director.id) if director else None
    prep = db.get(Record, 'prep_' + director.id) if director else None
    asset_ids = [] if frozen_bindings else list((visual.data.get('bindings', {}) if visual else {}).get(shot_id, []))
    for asset_id in asset_ids:
        asset = db.get(Record, asset_id)
        expected = (visual.data.get('asset_versions', {}) if visual else {}).get(asset_id)
        if not asset or asset.kind != 'asset' or asset.data.get('status') != 'approved' or asset.version != expected:
            raise HTTPException(409, '该镜头的参考资产已经变化，请重新发布作者版本。')
        frozen = snapshot_asset(db, asset, expected)
        bindings.append({'role': 'locked_reference', 'asset_revision_id': frozen.id, **frozen.data})
        spec = (prep.data.get('assets', {}) if prep else {}).get(asset_id, {})
        locked.append({'asset_id': asset_id, 'role': spec.get('role') or asset.data.get('type'),
                       'name': spec.get('name') or asset.data.get('name'),
                       'notes': (spec.get('notes') or asset.data.get('description') or '')[:500]})
    unique = []
    for item in bindings:
        media = (item.get('file') or {}).get('media')
        if media and not any((saved.get('file') or {}).get('media') == media for saved in unique):
            unique.append(item)
    if not unique or len(unique) > 9:
        raise HTTPException(422, '分支需要 1–9 张当前镜头参考图。')
    return unique, locked, director, shot_id


def _planning_context(release, director, shot_id, source_entry, original_index, min_rejoin_index,
                      at_end, history, intent, locked):
    board = (director.data.get('board') or {}).get('shots', []) if director else []
    shot_map = {shot.get('id'): shot for shot in board}
    original_entries = release.data.get('entries', [])
    current = shot_map.get(shot_id, {})
    current_scene = current.get('scene')
    candidates = [] if at_end else [
        {'index': index, **{key: shot_map.get(entry.get('shot_id'), {}).get(key)
                           for key in ('id', 'scene', 'purpose', 'dramatic_action', 'continuity_in',
                                       'continuity_out', 'viewer_knows', 'character_knows', 'dialogue')}}
        for index, entry in enumerate(original_entries[min_rejoin_index:], min_rejoin_index)
        if not current_scene or shot_map.get(entry.get('shot_id'), {}).get('scene') == current_scene
    ][:3]
    treatment = director.data.get('treatment', {}) if director else {}
    return {
        'story': {'title': release.data.get('title'), 'premise': release.data.get('description'),
                  'rules': treatment.get('rules', []), 'boundaries': treatment.get('boundaries', [])},
        'cut': {'original_index': original_index, 'at_story_end': at_end,
                'active_branch_segment': source_entry.get('summary'),
                'current_shot': {key: current.get(key) for key in ('id', 'scene', 'purpose', 'dramatic_action',
                    'continuity_in', 'continuity_out', 'viewer_knows', 'character_knows', 'dialogue')}},
        'history': history, 'reader_intent': intent,
        'locked_visual': {'assets': locked, 'rule': '只可复用当前场景、当前人物与当前着装，不创建或替换任何视觉资产。'},
        'candidate_rejoins': candidates,
    }


def _plan_contract(raw, *, min_rejoin_index, entry_count, at_end, max_shots, allowed_rejoins=None):
    plan = BranchPlan.model_validate(raw)
    if not plan.accepted:
        return plan
    if len(plan.shots) > max_shots:
        raise ValueError(f'当前分支最多使用 {max_shots} 个轻量镜头。')
    expected = [f'B{index + 1:02d}' for index in range(len(plan.shots))]
    if [shot.id for shot in plan.shots] != expected:
        raise ValueError('分支镜头必须从 B01 连续编号。')
    for shot in plan.shots:
        visual_action = shot.action + shot.continuity_in + shot.continuity_out
        if FORBIDDEN_VISUAL_CHANGE.search(visual_action):
            raise ValueError(f'{shot.id} 包含新场景、人物或换装要求，超出锁定视觉范围。')
    if at_end and (not plan.terminal or plan.rejoin_index is not None):
        raise ValueError('故事末尾的改写必须先完成因果，不能强行回到原结局。')
    if plan.rejoin_index is not None and not min_rejoin_index <= plan.rejoin_index < entry_count:
        raise ValueError('回归位置必须是暂停点之后存在的原版镜头。')
    if plan.rejoin_index is not None and allowed_rejoins is not None and plan.rejoin_index not in allowed_rejoins:
        raise ValueError('回归镜头与当前锁定场景或因果入口不兼容。')
    return plan


@router.post('/branches')
def create_branch(body: BranchCommand, x_reader_session: str = Header(alias='X-Reader-Session')):
    session_id = _session(x_reader_session)
    intent = body.text.strip()
    if len(intent) < 2:
        raise HTTPException(422, '请至少写两个有效字符。')
    cfg = settings()
    if not body.confirm_generation:
        raise HTTPException(422, '请确认生成读者分支。')
    refusal = paid_gate(cfg, 'llm', 'video')
    if refusal:
        raise HTTPException(422, refusal)
    if model_config().get('VIDEO_PROVIDER', 'ark') != 'minimax':
        raise HTTPException(422, '当前轻量分支需要支持多参考图的 MiniMax H3 视频模型。')
    branch_id = uid('branch')
    with Session.begin() as db:
        release = _release(db, body.release_id)
        source_entry, original_index, at_end, history, parent, min_rejoin_index = _resolve_cut(
            db, body, session_id, release)
        start, end = source_entry.get('start'), source_entry.get('end')
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (start, end)) or not start <= body.offset <= end:
            raise HTTPException(422, '暂停位置无效。')
        source = media_path(source_entry.get('media'))
        frame_at = min(body.offset, max(start, end - 0.05))
        frame_path = DATA / 'media' / 'reader_branches' / branch_id / 'cut.png'
        extract_frame_at_second(source, frame_at, frame_path)
        cut_file = file_info(frame_path, 'image/png')
        bindings, locked, director, original_shot_id = _visual_context(db, release, source_entry, original_index, cut_file)
        context = _planning_context(release, director, original_shot_id, source_entry, original_index,
                                    min_rejoin_index, at_end, history, intent, locked)
        version = (parent.data.get('branch_version', 0) + 1) if parent else 1
        branch = Record(id=branch_id, kind='reader_branch', data={
            'schema_version': 1, 'reader_session_id': session_id, 'release_id': release.id,
            'base_branch_id': parent.id if parent else None, 'branch_version': version,
            'intent': intent, 'status': 'planning', 'created_at': time.time(),
            'cut': {'manifest_index': body.index, 'offset': body.offset, 'original_index': original_index,
                    'original_shot_id': original_shot_id, 'source_kind': source_entry.get('kind', 'original'),
                    'source_occurrence_id': source_entry.get('occurrence_id'), 'at_story_end': at_end},
            'reference_bindings': bindings, 'locked_visual': locked,
        })
        plan_task = Task(id=uid('branchplan'), kind='reader_branch_plan', message='正在快速梳理选择、后果与可用回归点', payload={
            'mode': 'live', 'title': '读者分支因果规划', 'reader_branch_id': branch.id,
            'min_rejoin_index': min_rejoin_index, 'entry_count': len(release.data.get('entries', [])),
            'at_story_end': at_end, 'max_shots': 3 if at_end else 2,
            'allowed_rejoins': [item['index'] for item in context['candidate_rejoins']], 'planning_input': context,
        })
        branch.data = {**branch.data, 'plan_task_id': plan_task.id}
        wish = Record(id=uid('wish'), kind='reader_wish', data={
            'release_id': release.id, 'branch_id': branch.id, 'reader_session_id': session_id,
            'index': body.index, 'offset': body.offset, 'text': intent,
            'status': 'planning', 'note': '正在生成轻量剧情分支。',
        })
        db.add_all([branch, plan_task, wish])
        db.flush()
        return _public_branch(db, branch)


@router.get('/branches/{branch_id}')
def get_branch(branch_id: str, x_reader_session: str = Header(alias='X-Reader-Session')):
    session_id = _session(x_reader_session)
    with Session() as db:
        branch = db.get(Record, branch_id)
        if not branch or branch.kind != 'reader_branch' or branch.data.get('reader_session_id') != session_id:
            raise HTTPException(404, '读者分支不存在。')
        return _public_branch(db, branch)


def run_plan(task_id, payload):
    with Session.begin() as db:
        task = db.get(Task, task_id)
        branch = db.get(Record, payload.get('reader_branch_id', ''))
        if not task or not branch or branch.kind != 'reader_branch':
            raise HTTPException(409, '读者分支规划记录不存在。')
        if branch.data.get('video_tasks'):
            return {'branch_id': branch.id, 'video_tasks': copy.deepcopy(branch.data['video_tasks'])}
        task.progress = 12
        task.message = '正在用快速模型检查选择后的直接后果与衔接条件'
    plan, _ = call_node(chat_json, 'reader_branch_plan',
        {**payload['planning_input'], 'schema': BranchPlan.model_json_schema()}, task_id, profile='fast',
        validator=lambda raw: _plan_contract(raw, min_rejoin_index=payload['min_rejoin_index'],
            entry_count=payload['entry_count'], at_end=payload['at_story_end'], max_shots=payload['max_shots'],
            allowed_rejoins=payload['allowed_rejoins']))
    with Session.begin() as db:
        branch = db.get(Record, payload['reader_branch_id'])
        if not branch or branch.kind != 'reader_branch':
            raise HTTPException(409, '读者分支已经停止。')
        if branch.data.get('video_tasks'):
            return {'branch_id': branch.id, 'video_tasks': copy.deepcopy(branch.data['video_tasks'])}
        if not plan.accepted:
            branch.data = {**branch.data, 'plan': plan.model_dump(), 'status': 'rejected', 'message': plan.conflict}
            branch.version += 1
            wish = next((row for row in db.scalars(select(Record).where(Record.kind == 'reader_wish'))
                         if row.data.get('branch_id') == branch.id), None)
            if wish:
                wish.data = {**wish.data, 'status': 'rejected', 'note': plan.conflict}
            return {'branch_id': branch.id, 'rejected': True}
        references = branch.data['reference_bindings']
        base_reference_media = [(item.get('file') or {}).get('media') for item in references]
        if not 1 <= len(base_reference_media) <= 9 or any(not media for media in base_reference_media):
            raise HTTPException(409, '分支参考图快照不完整。')
        for item in references:
            verify_file(item['file'])
        release = _release(db, branch.data['release_id'])
        visual=db.get(Record,'visual_'+release.data.get('director_id',''))
        branch_style=visual.data.get('style') if visual else None
        tasks = []
        for index, shot in enumerate(plan.shots):
            shot_references = copy.deepcopy(references)
            if index == len(plan.shots) - 1 and plan.rejoin_index is not None and len(shot_references) < 9:
                target = release.data['entries'][plan.rejoin_index].get('boundaries', {}).get('first')
                if isinstance(target, dict) and target.get('media') not in base_reference_media:
                    verify_file(target)
                    shot_references.append({'role': 'rejoin_frame', 'name': '原版回归入口画面', 'file': copy.deepcopy(target)})
            reference_media = [(item.get('file') or {}).get('media') for item in shot_references]
            reference_description = '\n'.join(
                f'参考图 {ref_index + 1}：{item.get("name") or "锁定视觉参考"}；'
                + ('仅用于匹配最后的原版衔接边界。' if item.get('role') == 'rejoin_frame'
                   else '只用于保持既有人物、着装与场景。')
                for ref_index, item in enumerate(shot_references))
            motion = (
                f'读者分支 {shot.id}。从上一段已完成状态自然接续。入口连续性：{shot.continuity_in}。'
                f'核心动作：{shot.action}。' + (f'对白：{shot.dialogue}。' if shot.dialogue else '')
                + f'必须清楚呈现的因果结果：{shot.causal_result}。结束状态：{shot.continuity_out}。'
                  '全程只使用参考图内已有的场景、人物和服装；不得换装、转场、增加人物或创造新视觉元素。'
            )
            continuity = ' '.join(part for part in (
                f'入口连续性：{shot.continuity_in}' if shot.continuity_in else '',
                f'结束状态：{shot.continuity_out}' if shot.continuity_out else '') if part)
            rendered = render_node('video_render', {'composition': continuity, 'motion': motion,
                                                    'references': reference_description,
                                                    'style': f'统一视觉：{branch_style}' if branch_style else '统一视觉：沿用所附参考图的既有画风。'})
            task = Task(id=uid('branchvideo'), kind='video', created=time.time() + index * 0.001, payload={
                'mode': 'live', 'input_mode': 'reference_images', 'title': '读者分支 ' + shot.id,
                'reader_branch_id': branch.id, 'reader_branch_version': branch.data['branch_version'],
                'branch_shot_id': shot.id, 'branch_shot_index': index,
                'base_shot_id': branch.data.get('cut', {}).get('original_shot_id'),
                'generation_seconds': shot.generation_seconds, 'edit_seconds': shot.generation_seconds,
                'reference_media': reference_media, **rendered,
            })
            task.payload['input_snapshot'] = {
                'schema_version': 1, 'snapshot_status': 'captured_reader_branch',
                'reader_branch': {'id': branch.id, 'version': branch.data['branch_version'], 'shot_id': shot.id},
                'asset_bindings': shot_references, 'prompt': task.payload['prompt'],
                'constraints': {'new_scene': False, 'new_character': False, 'costume_change': False},
            }
            db.add(task)
            tasks.append({'shot_id': shot.id, 'task_id': task.id})
        branch.data = {**branch.data, 'plan': plan.model_dump(), 'video_tasks': tasks,
                       'status': 'generating', 'planned_at': time.time()}
        branch.version += 1
        parent_id = branch.data.get('base_branch_id')
        parent = db.get(Record, parent_id) if parent_id else None
        if parent and parent.kind == 'reader_branch':
            parent.data = {**parent.data, 'superseded_by': branch.id}
            parent.version += 1
        wish = next((row for row in db.scalars(select(Record).where(Record.kind == 'reader_wish'))
                     if row.data.get('branch_id') == branch.id), None)
        if wish:
            wish.data = {**wish.data, 'status': 'generating', 'note': plan.summary}
        return {'branch_id': branch.id, 'video_tasks': tasks}
