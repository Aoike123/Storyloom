// 来源区数据模型（A1）：正文版本 + 段落 + 故事片段（连续、非空、不重叠的荧光区域）。
// 本轮无正式精确文字范围契约（D03 待细化）：真实原文来自 work.source.content；
// 片段在缺失后端结构时用隔离示例推导（isExample=true，绝不写入项目），仅用于确认交互。
import { type Workspace } from './workspace-state';

export type SourceView = 'original' | 'fragments';

export interface FragmentRange {
  start: number; // 半开区间 [start, end)，正文字符偏移
  end: number;
}

export interface SourceFragment {
  id: string; // 稳定片段 id（F01、F02…）
  name: string;
  status: '已确认' | '待确认';
  origin: string; // 切分来源：用户切分 / AI 建议 · 用户确认 / AI 切分建议
  progress: string; // 产物与覆盖的简短进度
  task: Workspace; // 点击该片段倾向进入的工作标签
  range: FragmentRange;
  revision: string; // 正文版本
  isExample?: boolean;
}

export interface SourceModel {
  revision: string; // 正文版本（导入原稿 hash 派生 / 'import-1'）
  text: string; // 完整原文（连续）
  paragraphs: string[]; // 按空行切分的段落（保留内部换行）
  fragments: SourceFragment[]; // 已标注的荧光区域（按来源顺序）
  isExample: boolean; // 整组片段是否为示例（真实项目无片段结构时为 true）
}

interface Span {
  text: string;
  start: number;
  end: number;
}

// 按“空行（连续 ≥2 换行）”切分正文，返回每段的原文与其在全文中的半开字符区间。
// 不用 display 段号或 DOM 坐标——偏移是全文真实字符位置（与 D03 范围契约方向一致）。
export function splitParagraphSpans(text: string): Span[] {
  const spans: Span[] = [];
  const n = text.length;
  let i = 0;
  while (i < n) {
    while (i < n && /\s/.test(text[i])) i++;
    if (i >= n) break;
    const start = i;
    while (i < n) {
      if (text[i] === '\n' && (i + 1 >= n || text[i + 1] === '\n')) break; // 遇空行结束本段
      i++;
    }
    spans.push({ text: text.slice(start, i), start, end: i });
    while (i < n && /\s/.test(text[i])) i++;
  }
  return spans;
}

// 从一段文字的首行派生一个可读片段名（示例用；真实名称来自项目对象）。
function nameFrom(spanText: string, fallback: string): string {
  const line = spanText.split('\n').find((l) => l.trim()) ?? '';
  const t = line.trim();
  if (!t) return fallback;
  return t.length > 14 ? t.slice(0, 14) + '…' : t;
}

// 用连续段落组推导隔离示例片段：每次 2 段为一块，最多 3 块（覆盖正文前部，尾部留作未标注，
// 便于演示“划选未标注文字建候选”）。全部标记 isExample，不写库、不暗示后端已支持片段结构。
export function computeExampleFragments(spans: Span[]): SourceFragment[] {
  if (spans.length === 0) return [];
  const n = spans.length;
  const maxFrags = Math.min(3, Math.ceil(n / 2));
  const out: SourceFragment[] = [];
  for (let f = 0; f < maxFrags; f++) {
    const a = f * 2;
    if (a >= n) break;
    const b = Math.min(n - 1, a + 1);
    const start = spans[a].start;
    const end = spans[b].end;
    const last = f === maxFrags - 1;
    out.push({
      id: 'F' + String(f + 1).padStart(2, '0'),
      name: nameFrom(spans[a].text, '示例片段 ' + String(f + 1).padStart(2, '0')),
      status: last ? '待确认' : '已确认',
      origin: f === 0 ? '用户切分' : f === 1 ? 'AI 建议 · 用户确认' : 'AI 切分建议',
      progress: last ? '片段边界待确认' : '剧本 2 项 · 视频 0/3',
      task: f === 0 ? 'script' : f === 1 ? 'assets' : 'cut',
      range: { start, end },
      revision: 'import-1',
      isExample: true,
    });
  }
  return out;
}

// 从完整原文构造 SourceModel：真实正文 + 段落 + 隔离示例片段（无后端片段结构时）。
export function buildSourceModel(text: string, revision: string): SourceModel {
  const trimmed = (text ?? '').trim();
  const spans = splitParagraphSpans(trimmed);
  const fragments = computeExampleFragments(spans);
  return {
    revision,
    text: trimmed,
    paragraphs: spans.map((s) => s.text),
    fragments,
    isExample: fragments.length > 0,
  };
}

// 划选范围校验（A2 用）：连续、非空、边界合法，且与已有片段不重叠（重复/部分相交/包含都阻止，
// 边界相接允许）。前端即时反馈与服务端最终校验方向一致。
export function checkRange(
  range: FragmentRange | null,
  text: string,
  existing: SourceFragment[],
  excludeId?: string,
): { valid: boolean; reason: string; conflicts: SourceFragment[] } {
  if (!range || !Number.isInteger(range.start) || !Number.isInteger(range.end) || range.start < 0 || range.end > text.length || range.start >= range.end) {
    return { valid: false, reason: '请在原文中划选一段连续文字。', conflicts: [] };
  }
  if (!text.slice(range.start, range.end).trim()) {
    return { valid: false, reason: '选区不能为空白。', conflicts: [] };
  }
  const conflicts = existing.filter((f) => f.id !== excludeId && range.start < f.range.end && range.end > f.range.start);
  return {
    valid: conflicts.length === 0,
    reason: conflicts.length ? '选区与 ' + conflicts.map((c) => c.id + '「' + c.name + '」').join('、') + ' 重叠，请在未标注文字中重新划选。' : '',
    conflicts,
  };
}

// 把正文切分为“间隙（未标注）/ 区域（片段或候选）”交替的有序段，供连续原文 + 荧光区域渲染。
// 区域段携带真实片段 id，用于点击跳转与高亮当前片段。
export interface RenderSegment {
  text: string;
  region?: { id: string; kind: 'fragment' | 'pending' };
}

export function renderSegments(text: string, fragments: SourceFragment[], pending?: FragmentRange | null): RenderSegment[] {
  const marks: { start: number; end: number; id: string; kind: 'fragment' | 'pending' }[] = fragments.map((f) => ({
    start: f.range.start,
    end: f.range.end,
    id: f.id,
    kind: 'fragment',
  }));
  if (pending && pending.end > pending.start) marks.push({ ...pending, id: 'pending', kind: 'pending' });
  marks.sort((a, b) => a.start - b.start || a.end - b.end);
  const out: RenderSegment[] = [];
  let cursor = 0;
  for (const m of marks) {
    if (m.start < cursor) continue; // 已被前一块覆盖，跳过
    if (m.start > cursor) out.push({ text: text.slice(cursor, m.start) });
    out.push({ text: text.slice(m.start, m.end), region: { id: m.id, kind: m.kind } });
    cursor = m.end;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor) });
  return out;
}
