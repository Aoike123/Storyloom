"""Playback state is derived from visible events, never from unplayed plans."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator

INITIAL = {'knife': 'attacker', 'heroine': 'alive', 'location': 'platform',
           'evidence': 'hidden', 'help': 'absent', 'case': 'open'}
ALLOWED = {
    'knife': {'attacker', 'heroine', 'ground', 'secured'},
    'heroine': {'alive', 'dead'}, 'location': {'platform', 'security'},
    'evidence': {'hidden', 'found', 'submitted'}, 'help': {'absent', 'called', 'present'},
    'case': {'open', 'resolved'},
}
LABELS = {'knife': '刀的位置', 'heroine': '林夏', 'location': '地点', 'evidence': '证据', 'help': '支援', 'case': '案件'}
VALUE_LABELS = {'attacker': '歹徒持有', 'heroine': '林夏持有', 'ground': '地面', 'secured': '已收存',
                'alive': '存活', 'dead': '死亡', 'platform': '旧站台', 'security': '值班室', 'hidden': '尚未发现',
                'found': '已找到', 'submitted': '已提交', 'absent': '尚未到场', 'called': '已呼叫',
                'present': '已到场', 'open': '未解决', 'resolved': '真相公开'}

class Beat(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    narration: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=600)
    changes: dict[str, str] = Field(default_factory=dict)
    duration: int = Field(default=8, ge=5, le=15)

class Plan(BaseModel):
    status: Literal['ready', 'conflict', 'needs_review']
    summary: str = Field(max_length=1000)
    preserved: list[str] = Field(default_factory=list, max_length=20)
    beats: list[Beat] = Field(default_factory=list, max_length=6)
    suggestions: list[str] = Field(default_factory=list, max_length=5)
    @model_validator(mode='after')
    def nonempty(self):
        if self.status == 'ready' and not self.beats:
            raise ValueError('A ready plan needs beats')
        return self

def state_at(entries, clips, index, offset):
    if not 0 <= index < len(entries):
        raise ValueError('播放片段不存在')
    state = dict(INITIAL)
    history = []
    for i, entry in enumerate(entries[:index + 1]):
        clip = clips[entry['clip_id']]
        start, end = entry.get('start', 0), entry.get('end', clip['duration'])
        if i == index and not start <= offset <= end:
            raise ValueError('暂停时间不在当前片段内')
        limit = offset if i == index else end
        for ev in clip.get('events', []):
            if start <= ev['at'] <= limit:
                state.update(ev['changes'])
                history.append(ev['text'])
    return state, history

def prefix_at(entries, index, offset):
    prefix = [dict(x) for x in entries[:index]]
    current = dict(entries[index])
    if offset > current.get('start', 0) + .01:
        current['end'] = offset
        prefix.append(current)
    return prefix

def validate_plan(plan: Plan, state):
    if plan.status != 'ready':
        return []
    errors, current = [], dict(state)
    for beat in plan.beats:
        for key, value in beat.changes.items():
            if key not in ALLOWED or value not in ALLOWED[key]:
                errors.append('状态变更包含未定义的字段或取值')
                continue
            if key == 'heroine' and value == 'dead':
                errors.append('本故事作者约束要求林夏活着提交证据')
            if key == 'evidence' and current['evidence'] == 'submitted' and value != 'submitted':
                errors.append('不能撤回已提交证据这一事实')
            if key == 'evidence' and current['evidence'] == 'hidden' and value == 'submitted':
                errors.append('尚未找到证据，不能直接提交')
            if key == 'knife' and current['knife'] == 'secured' and value != 'secured':
                errors.append('已收存的刀不能无解释重新出现')
            if key == 'help' and current['help'] == 'absent' and value == 'present':
                errors.append('需要先交代支援如何被呼叫')
            if key == 'case' and value == 'resolved' and current['evidence'] != 'submitted':
                errors.append('证据提交之前不能直接结束案件')
        current.update(beat.changes)
    if current != JOIN_STATE:
        errors.append('过渡结束状态尚不满足原版尾声的接入条件')
    return list(dict.fromkeys(errors))

JOIN_STATE = {'knife': 'secured', 'heroine': 'alive', 'location': 'security',
              'evidence': 'submitted', 'help': 'present', 'case': 'open'}

def demo_plan(text, state):
    """A deliberately limited deterministic fixture, not claimed as AI reasoning."""
    if any(x in text for x in ['死亡', '死了', '杀死女主', '毁掉证据', '销毁证据', '瞬移', '复活']):
        return Plan(status='conflict', summary='这次修改与作者结局或当前世界规则冲突。演示规则不会强行覆盖它。',
                    suggestions=['让林夏暂时脱险，再寻找证据', '通过站台紧急按钮呼叫支援'])
    if not any(x in text for x in ['夺', '刀', '报警', '求助', '呼叫', '躲', '逃', '证据']):
        return Plan(status='needs_review', summary='演示规则只支持夺刀、逃脱、求助和寻找证据。自由改写需要接入真实语言模型。',
                    suggestions=['林夏侧身躲开，夺下歹徒的刀', '林夏按下站台紧急按钮呼叫支援'])
    if '夺' in text and state['knife']!='attacker':
        return Plan(status='needs_review',summary='此刻歹徒已经不再持刀，不能重复夺刀。可以从当前状态采取新行动，或回到更早时刻改写。',suggestions=['林夏呼叫支援','林夏寻找证据'])
    current = dict(state)
    beats = []
    def add(title, narration, reason, changes):
        changes = {k: v for k, v in changes.items() if current[k] != v}
        if changes:
            beats.append(Beat(title=title, narration=narration, reason=reason, changes=changes))
            current.update(changes)
    if any(x in text for x in ['夺', '刀']) and current['knife'] == 'attacker':
        add('改变发生了', '林夏借立柱避开刀锋，抓住对方失衡的一瞬夺下刀，退到长椅另一侧。',
            '站台立柱与长椅提供阻隔；歹徒扑空后失去平衡。', {'knife': 'heroine'})
    elif current['knife'] == 'attacker':
        add('拉开距离', '林夏绕到长椅另一侧，与歹徒保持距离，借遮挡按下站台紧急按钮。',
            '已设定的长椅与紧急按钮提供逃脱和求助条件。', {'help': 'called'})
    add('求助信号', '林夏按下紧急按钮，向值班室说清站台位置与危险情况。',
        '站台紧急按钮连通值班室，支援有了明确来源。', {'help': 'called'} if current['help'] == 'absent' else {})
    add('值班员到场', '值班员沿楼梯赶来，与林夏保持安全距离，隔开歹徒并收存刀具。',
        '此前已发出求助；值班室就在站台上层。', {'help': 'present', 'knife': 'secured'})
    add('找到证据', '现场安全后，林夏检查长椅下的牛皮纸袋，找到记录交易时间的账本。',
        '原版开场已给出长椅下纸袋这一环境线索。', {'evidence': 'found'} if current['evidence'] == 'hidden' else {})
    add('回到作者主线', '林夏带着账本走进值班室，向到场人员提交证据。夺刀或逃脱的经过保留在她的陈述里。',
        '危险解除、证据找到后，提交证据有连续的行动依据。', {'location': 'security', 'evidence': 'submitted'})
    if not beats:
        return Plan(status='needs_review', summary='当前已经满足尾声条件。此演示不再扩展结局，请回到较早时刻改写。')
    return Plan(status='ready', summary='先响应你的行动，再通过求助、取证与提交证据接回尾声。此方案来自有限演示规则。',
                preserved=[f'{LABELS[k]}：{VALUE_LABELS[v]}' for k, v in state.items()], beats=beats)
