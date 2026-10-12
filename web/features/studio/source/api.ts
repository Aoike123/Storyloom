// SOURCE-12 · 连续原文与动态片段视图 — 纯逻辑 API 模块（契约 v2.9）。
//
// 冻结约束：
// - 纯逻辑模块：禁止 import React/JSX；运行时零依赖（类型导入全部 type-only，
//   transpile 后无 require），可独立 vm 测试；
// - DTO 逐字对齐后端：
//   · ProjectDto = P3 七字段（backend/studio/projects/service.py _to_dto；
//     description 模型 NOT NULL default ''）；
//   · SourceRevisionDto = S3 读取返回（backend/studio/sources/service.py
//     get_source_revision：§1 全字段 + is_active；S1 专有的
//     activation_hint/duplicate_content 不在 S3 返回中）；
//   · FragmentDto = F2 列表项 / F3 详情共用十一键
//     （backend/studio/sources/fragment_service.py _fragment_dto；
//     state = 有效状态：retired 恒 'retired'；绑定版本非 active 时派生
//     'pending_review'（不落库）；F2 只列 active 版本 → 实际只见持久态）；
//   · FragmentListDto = {items, next_cursor}（附录 00 §6 查询 DTO）。
// - 所有函数第一参 = createClient 风格客户端对象，返回其
//   StudioApiResponse<T>；错误透传不吞（不 try/catch、不改写）。
// - fetchAllFragments = F2 全量聚合：跟随 next_cursor 翻页，页级错误原样
//   透传；仅死循环防御（页数上限 / 游标不前进）新造 internal_error 返回，
//   绝不静默返回部分数据（防 >200 片段时的高亮缺口）。
// - 路由前缀 /api/studio（backend/studio/router.py 冻结）。

import type { createClient } from "../../../shared/api/client";
import type { StudioApiResponse } from "../../../shared/api/types";

/** createClient 风格客户端对象（第一参注入；测试用真客户端 + mock fetch）。 */
export type StudioClient = ReturnType<typeof createClient>;

/** P3 项目 DTO（七字段）。 */
export interface ProjectDto {
  id: string;
  name: string;
  description: string;
  visibility: string;
  active_source_revision_id: string | null;
  created_at: number;
  updated_at: number;
}

/** S3 来源修订 DTO（§1 全字段 + is_active）。 */
export interface SourceRevisionDto {
  id: string;
  project_id: string;
  previous_revision_id: string | null;
  raw_content: string;
  raw_hash: string;
  canonical_content: string;
  canonical_hash: string;
  offset_policy: string;
  char_length: number;
  created_at: number;
  is_active: boolean;
}

/** 片段有效状态（附录 02 §1：pending_review 为读时派生值，不落库）。 */
export type FragmentState =
  | "candidate"
  | "confirmed"
  | "retired"
  | "pending_review";

/** F2 列表项 / F3 详情共用片段 DTO（十一键）。 */
export interface FragmentDto {
  object_ref: { kind: string; id: string; revision: number };
  name: string;
  summary: string | null;
  state: FragmentState;
  revision_is_active: boolean;
  range: { start: number; end: number };
  source_revision_id: string;
  predecessor_ids: string[];
  created_at: number;
  updated_at: number;
  retired_at: number | null;
}

/** F2 片段列表响应。 */
export interface FragmentListDto {
  items: FragmentDto[];
  next_cursor: string | null;
}

/** P3 读取单项目：GET /api/studio/projects/{pid}。 */
export function getProject(
  client: StudioClient,
  pid: string,
): Promise<StudioApiResponse<ProjectDto>> {
  return client.get<ProjectDto>(`/api/studio/projects/${encodeURIComponent(pid)}`);
}

/** S3 读取来源版本：GET /api/studio/projects/{pid}/sources/{revisionId}。 */
export function getSourceRevision(
  client: StudioClient,
  pid: string,
  revisionId: string,
): Promise<StudioApiResponse<SourceRevisionDto>> {
  return client.get<SourceRevisionDto>(
    `/api/studio/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(revisionId)}`,
  );
}

export interface ListFragmentsOptions {
  cursor?: string;
  state?: string;
  limit?: number;
}

/** query 组装：仅在有值时附加（undefined/空串跳过）；键值均 encodeURIComponent。 */
function buildQuery(params: Array<[string, string | number | undefined]>): string {
  const parts: string[] = [];
  for (const [key, value] of params) {
    if (value === undefined || value === "") continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length === 0 ? "" : `?${parts.join("&")}`;
}

/** F2 片段列表：GET /api/studio/projects/{pid}/fragments[?state=&limit=&cursor=]。 */
export function listFragments(
  client: StudioClient,
  pid: string,
  opts?: ListFragmentsOptions,
): Promise<StudioApiResponse<FragmentListDto>> {
  const query = buildQuery([
    ["state", opts?.state],
    ["limit", opts?.limit],
    ["cursor", opts?.cursor],
  ]);
  return client.get<FragmentListDto>(
    `/api/studio/projects/${encodeURIComponent(pid)}/fragments${query}`,
  );
}

/** fetchAllFragments 聚合选项（cursor 由函数内部管理，不开放）。 */
export interface FetchAllFragmentsOptions {
  /** 按有效状态过滤（默认全部 state）。 */
  state?: string;
  /** 每页 limit（默认 200 = 服务端 clamp 上限）。 */
  limit?: number;
  /** 翻页安全上限（默认 50 页）：超限返回错误而非死循环。 */
  maxPages?: number;
}

/**
 * F2 全量聚合：跟随 next_cursor 翻页直到 null，按页序拼接返回全部片段
 * （SourceReader 与 FragmentList 共用，消除 >200 片段时两视图取数不一致）。
 *
 * - 页级错误原样透传（不吞不改写），绝不静默返回已拉到的部分数据；
 * - 死循环防御：maxPages 页数上限（默认 50）、next_cursor 不前进即停——
 *   两种异常都新造 internal_error 返回（宁可报错也不错缺高亮）。
 */
export async function fetchAllFragments(
  client: StudioClient,
  pid: string,
  opts?: FetchAllFragmentsOptions,
): Promise<StudioApiResponse<FragmentDto[]>> {
  const limit = opts?.limit ?? 200;
  const maxPages = opts?.maxPages ?? 50;
  const items: FragmentDto[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < maxPages; page++) {
    const res = await listFragments(client, pid, {
      state: opts?.state,
      limit,
      cursor,
    });
    if (!res.ok) return res;
    items.push(...res.data.items);
    const next = res.data.next_cursor;
    if (next === null || next === "") return { ok: true, data: items };
    if (next === cursor) {
      return {
        ok: false,
        code: "internal_error",
        message: "片段列表分页异常（游标未前进），请重试。",
        details: { reason: "cursor_not_advancing", cursor: next },
      };
    }
    cursor = next;
  }
  return {
    ok: false,
    code: "internal_error",
    message: "片段列表分页异常（页数超限），请重试。",
    details: { reason: "max_pages_exceeded", max_pages: maxPages },
  };
}

/**
 * 阅读视图连续段。索引 = JS 字符串索引（UTF-16 code unit，与后端
 * offset_policy 'lf-utf16-v1' 一致）；切片用 String.prototype.slice。
 *
 * 规则（冻结）：
 * - 非退役片段范围 → 高亮段（fragmentId/state 为真实服务端值）；
 *   其余区间 → 普通段（fragmentId/state = null）；
 * - 服务端保证范围不重叠，本函数仍按 start 升序排序后计算；
 * - 相接边界不产生空段；空文本返回 []；
 * - 防御：空范围（end<=start）跳过、端点夹取到 [0, len]、与已产出段
 *   重叠者跳过（服务端保证不发生，发生时宁可不高亮也不错切）。
 */
export interface ReaderSegment {
  start: number;
  end: number;
  fragmentId: string | null;
  state: string | null;
}

export function computeSegments(
  canonical: string,
  fragments: FragmentDto[],
): ReaderSegment[] {
  if (canonical.length === 0) return [];
  const highlights = fragments
    .filter((f) => f.state !== "retired" && f.range.end > f.range.start)
    .map((f) => ({
      start: f.range.start,
      end: f.range.end,
      fragmentId: f.object_ref.id,
      state: f.state as string,
    }))
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const segments: ReaderSegment[] = [];
  let pos = 0;
  for (const h of highlights) {
    const start = Math.max(h.start, 0);
    const end = Math.min(h.end, canonical.length);
    if (end <= start || start < pos) continue;
    if (start > pos) {
      segments.push({ start: pos, end: start, fragmentId: null, state: null });
    }
    segments.push({ start, end, fragmentId: h.fragmentId, state: h.state });
    pos = end;
  }
  if (pos < canonical.length) {
    segments.push({ start: pos, end: canonical.length, fragmentId: null, state: null });
  }
  return segments;
}

// ---------------------------------------------------------------------------
// SOURCE-13 · 原生选区建立候选（契约 v2.9，附录 02 §2 F1）
// ---------------------------------------------------------------------------

/**
 * DOM 选区锚点（相对 .source-reader__text 容器 children 顺序）。
 * - nodeIndex = 选区锚点所在文本节点所属子元素（容器 children 顺序）的索引；
 * - offset = 该文本节点内的 UTF-16 偏移。
 * - nodeIndex 合法域 [0, texts.length]：nodeIndex === texts.length 表示
 *   “最后一个子元素之后”（offset 必须为 0）。
 */
export interface NodeAnchor {
  nodeIndex: number;
  offset: number;
}

/**
 * 把 DOM 锚点换算为全局 UTF-16 偏移。
 * - texts = 各段文本（按渲染顺序），拼接即完整 canonical；
 * - nodeIndex 非整数或不在 [0, texts.length] → null；
 * - offset 非整数或不在 [0, texts[nodeIndex].length]
 *   （nodeIndex === texts.length 时 offset 必须 0）→ null；
 * - 合法 → 前缀和（texts[0..nodeIndex-1] 的 length 之和）+ offset。
 */
export function anchorToIndex(texts: string[], anchor: NodeAnchor): number | null {
  const { nodeIndex, offset } = anchor;
  if (!Number.isInteger(nodeIndex) || nodeIndex < 0 || nodeIndex > texts.length) {
    return null;
  }
  if (nodeIndex === texts.length) {
    if (!Number.isInteger(offset) || offset !== 0) return null;
    let total = 0;
    for (const t of texts) total += t.length;
    return total;
  }
  if (!Number.isInteger(offset) || offset < 0 || offset > texts[nodeIndex].length) {
    return null;
  }
  let prefix = 0;
  for (let i = 0; i < nodeIndex; i++) prefix += texts[i].length;
  return prefix + offset;
}

/**
 * 把 DOM 选区（start/end 锚点）换算为半开区间 [start, end)。
 * - 两端各自 anchorToIndex；任一端 null → null；
 * - start > end（反向选区）→ 交换；start === end（collapsed）→ null；
 * - 否则返回 {start, end}。
 */
export function selectionToRange(
  texts: string[],
  start: NodeAnchor,
  end: NodeAnchor,
): { start: number; end: number } | null {
  const startIdx = anchorToIndex(texts, start);
  const endIdx = anchorToIndex(texts, end);
  if (startIdx === null || endIdx === null) return null;
  if (startIdx > endIdx) return { start: endIdx, end: startIdx };
  if (startIdx === endIdx) return null;
  return { start: startIdx, end: endIdx };
}

/** F1 候选片段创建成功体（201；幂等重放 200 同形）。 */
export interface FragmentCreateDto {
  object_ref: { kind: string; id: string; revision: number };
  name: string;
  summary: string | null;
  state: string;
  range: { start: number; end: number };
  created_at: number;
}

/** F1 候选片段创建参数。 */
export interface CreateFragmentCandidateParams {
  sourceRevisionId: string;
  start: number;
  end: number;
  name: string;
  commandId: string;
}

/**
 * F1 原子保存候选片段（附录 02 §2 F1）。
 *
 * - 首发：POST /api/studio/projects/{pid}/fragments，body 显式带
 *   expected_range_set_revision: null；
 * - 首发返回 ok=false 且 code === "revision_conflict" 且
 *   details.object.kind === "range_set" 且 details.actual 为 number →
 *   用相同 commandId 与相同其余 body 重试恰一次，body 的
 *   expected_range_set_revision 改为 details.actual；
 * - 重试结果（成功或失败）直接返回，不得第三次请求；
 * - 其余一切错误（range_overlap/validation_failed/range_invalid/not_found/
 *   network/…）原样透传，不重试、不改写、不静默裁切范围；
 * - 成功（201 或幂等重放 200）返回 res；FragmentCreateDto.object_ref.id
 *   是片段 ID 的唯一来源。
 */
export async function createFragmentCandidate(
  client: StudioClient,
  pid: string,
  params: CreateFragmentCandidateParams,
): Promise<StudioApiResponse<FragmentCreateDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments`;
  const baseBody = {
    source_revision_id: params.sourceRevisionId,
    range: { start: params.start, end: params.end },
    name: params.name,
    expected_range_set_revision: null,
  };
  const first = await client.post<FragmentCreateDto>(path, baseBody, {
    commandId: params.commandId,
  });
  if (!first.ok) {
    const details = first.details as Record<string, unknown> | undefined;
    const object = details?.object as { kind?: string } | undefined;
    const actual = details?.actual;
    if (
      first.code === "revision_conflict" &&
      object?.kind === "range_set" &&
      typeof actual === "number"
    ) {
      const retryBody = {
        ...baseBody,
        expected_range_set_revision: actual,
      };
      return client.post<FragmentCreateDto>(path, retryBody, {
        commandId: params.commandId,
      });
    }
  }
  return first;
}

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段确认（契约 v2.9，附录 02 §2 F6）
// ---------------------------------------------------------------------------

/** F6 片段确认参数。 */
export interface ConfirmFragmentParams {
  expectedRevision: number;
  commandId: string;
}

/**
 * F6 确认片段（附录 02 §2 F6；确认不启动生产、不表示全文覆盖，R04）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/confirm，body =
 *   { expected_revision, command_id }（command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res；FragmentDto 复用既有接口
 *   （F3 形状十一键，state='confirmed'）；
 * - 一切错误原样透传（不重试、不改写）：409 revision_conflict
 *   （details.object.kind === 'fragment'）、422 precondition_failed
 *   （details.reason）、422 validation_failed（details.violations）、
 *   404 not_found、network。
 */
export function confirmFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: ConfirmFragmentParams,
): Promise<StudioApiResponse<FragmentDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/confirm`;
  return client.post<FragmentDto>(
    path,
    { expected_revision: params.expectedRevision },
    { commandId: params.commandId },
  );
}

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段改名（契约 v2.9，附录 02 §2 F4）
// ---------------------------------------------------------------------------

/** F4 片段改名参数。 */
export interface RenameFragmentParams {
  name: string;
  expectedRevision: number;
  commandId: string;
}

/**
 * F4 片段就地改名（附录 02 §2 F4；ID 不变、改名不产生 revision、
 * 不动 range_set cas）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/rename，body =
 *   { name, expected_revision, command_id }（command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res；FragmentDto 复用既有接口
 *   （F3 形状十一键，name=trimmed，ID 不变）；
 * - 一切错误原样透传（不重试、不改写）：409 revision_conflict
 *   （details.object.kind === 'fragment'）、422 validation_failed
 *   （details.violations：name required/max_length、command_id reused）、
 *   422 precondition_failed（details.reason）、404 not_found、network。
 */
export function renameFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: RenameFragmentParams,
): Promise<StudioApiResponse<FragmentDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/rename`;
  return client.post<FragmentDto>(
    path,
    { name: params.name, expected_revision: params.expectedRevision },
    { commandId: params.commandId },
  );
}

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段退役（契约 v2.9，附录 02 §2 F7）
// ---------------------------------------------------------------------------

/** F7 退役预览参数。 */
export interface RetirePreviewParams {
  targetFragmentId: string;
  commandId: string;
}

/**
 * F7 退役预览响应（附录 00 §5.1：恰 4 字段，preview 是一次幂等写，
 * 记录 state=pending 不外露）。
 */
export interface RetirePreviewDto {
  preview_id: string;
  kind: string;
  baseline: {
    fragment: { kind: string; id: string; revision: number };
    range_set: { id: string | null; cas: number | null };
  };
  impact: {
    affected: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    needs_review: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    preservable: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
  };
}

/**
 * F7 预览退役片段（附录 02 §2 F7；preview 是一次幂等写，记录
 * state=pending 不外露；幂等重放命中 → 200 原 result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/retire，body =
 *   { target_fragment_id, command_id }（target_fragment_id 传路径 fid 值；
 *   command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res（恰 4 字段 {preview_id, kind,
 *   baseline, impact}）；
 * - 一切错误原样透传（不重试、不改写）：422 precondition_failed
 *   （details.reason：source_revision_inactive | already_retired）、
 *   422 validation_failed（details.violations：rule=mismatch | rule=reused）、
 *   404 not_found、network。
 */
export function previewRetireFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: RetirePreviewParams,
): Promise<StudioApiResponse<RetirePreviewDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/retire`;
  return client.post<RetirePreviewDto>(
    path,
    { target_fragment_id: params.targetFragmentId },
    { commandId: params.commandId },
  );
}

/** F7 退役应用参数。 */
export interface RetireApplyParams {
  previewId: string;
  commandId: string;
  expectedRevision: number;
  expectedRangeSetRevision: number;
}

/**
 * F7 应用退役片段（附录 02 §2 F7；退役只置状态，不删除任何产物——
 * R11 失效≠删除；退役不动 range_set cas；幂等重放命中 → 200 原
 * result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/retire/apply，body =
 *   { preview_id, command_id, expected_revision, expected_range_set_revision }
 *   （command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res（FragmentDto 复用既有接口：
 *   state='retired'、retired_at）；
 * - 一切错误原样透传（不重试、不改写）：409 preview_stale
 *   （details.preview_id + details.conflicts）、409 revision_conflict
 *   （details.object.kind：fragment | range_set）、422 validation_failed
 *   （violations：mismatch | reused）、422 precondition_failed、
 *   404 not_found、network。
 */
export function applyRetireFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: RetireApplyParams,
): Promise<StudioApiResponse<FragmentDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/retire/apply`;
  return client.post<FragmentDto>(
    path,
    {
      preview_id: params.previewId,
      expected_revision: params.expectedRevision,
      expected_range_set_revision: params.expectedRangeSetRevision,
    },
    { commandId: params.commandId },
  );
}

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段边界调整（契约 v2.9，附录 02 §2 F8）
// ---------------------------------------------------------------------------

/** F8 边界预览参数。 */
export interface BoundaryPreviewParams {
  targetFragmentId: string;
  newRange: { start: number; end: number };
  commandId: string;
}

/**
 * F8 边界预览响应（附录 00 §5.1：恰 4 字段，preview 是一次幂等写，
 * 记录 state=pending 不外露；range/重叠/版本非 active 均在 preview 阶段即校验）。
 */
export interface BoundaryPreviewDto {
  preview_id: string;
  kind: string;
  baseline: {
    fragment: { kind: string; id: string; revision: number };
    range_set: { id: string | null; cas: number | null };
  };
  impact: {
    affected: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    needs_review: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    preservable: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
  };
}

/**
 * F8 预览片段边界变更（附录 02 §2 F8；preview 是一次幂等写，记录
 * state=pending 不外露；幂等重放命中 → 200 原 result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/boundary，body =
 *   { target_fragment_id, new_range: {start, end}, command_id }
 *   （command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res（恰 4 字段 {preview_id, kind,
 *   baseline, impact}）；
 * - 一切错误原样透传（不重试、不改写）：422 range_invalid
 *   （details.rule：empty | out_of_bounds | proxy_split | blank |
 *   non_integer，message 含 canonical 长度）、409 range_overlap
 *   （details.conflicts）、422 precondition_failed（details.reason）、
 *   422 validation_failed（details.violations：mismatch | reused）、
 *   404 not_found、network。
 */
export function previewBoundaryFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: BoundaryPreviewParams,
): Promise<StudioApiResponse<BoundaryPreviewDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/boundary`;
  return client.post<BoundaryPreviewDto>(
    path,
    {
      target_fragment_id: params.targetFragmentId,
      new_range: { start: params.newRange.start, end: params.newRange.end },
    },
    { commandId: params.commandId },
  );
}

/** F8 边界应用参数。 */
export interface BoundaryApplyParams {
  previewId: string;
  commandId: string;
  expectedRevision: number;
  expectedRangeSetRevision: number;
}

/**
 * F8 应用片段边界变更（附录 02 §2 F8；新 FragmentRevision reason='boundary'、
 * ID 不变、revision+1、range=new_range；range_set 是 CAS 写点，cas+1；
 * 幂等重放命中 → 200 原 result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/boundary/apply，body =
 *   { preview_id, command_id, expected_revision, expected_range_set_revision }
 *   （command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res（FragmentDto 复用既有接口：
 *   range=new_range、object_ref.revision+1）；
 * - 一切错误原样透传（不重试、不改写）：409 preview_stale
 *   （details.preview_id + details.conflicts）、409 revision_conflict
 *   （details.object.kind：fragment | range_set）、422 validation_failed
 *   （violations：mismatch | reused）、422 precondition_failed、
 *   404 not_found、network。
 */
export function applyBoundaryFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: BoundaryApplyParams,
): Promise<StudioApiResponse<FragmentDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/boundary/apply`;
  return client.post<FragmentDto>(
    path,
    {
      preview_id: params.previewId,
      expected_revision: params.expectedRevision,
      expected_range_set_revision: params.expectedRangeSetRevision,
    },
    { commandId: params.commandId },
  );
}

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段拆分（契约 v2.9，附录 02 §2 F9）
// ---------------------------------------------------------------------------

/** F9 拆分预览参数。 */
export interface SplitPreviewParams {
  targetFragmentId: string;
  splitPoint: number;
  leftName?: string;
  rightName?: string;
  commandId: string;
}

/**
 * F9 拆分预览响应（附录 00 §5.1：恰 4 字段，preview 是一次幂等写，
 * 记录 state=pending 不外露；命名派生与 split_point 五判定均在
 * preview 阶段即校验——F9 行冻结）。
 */
export interface SplitPreviewDto {
  preview_id: string;
  kind: string;
  baseline: {
    fragment: { kind: string; id: string; revision: number };
    range_set: { id: string | null; cas: number | null };
  };
  impact: {
    affected: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    needs_review: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    preservable: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
  };
}

/**
 * F9 预览片段拆分（附录 02 §2 F9；preview 是一次幂等写，记录
 * state=pending 不外露；幂等重放命中 → 200 原 result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/split，body =
 *   { target_fragment_id, split_point, command_id }（command_id 由
 *   client.post 合并进 body）；left_name/right_name 仅在非空时附加；
 * - 成功（200，含幂等重放）返回 res（恰 4 字段 {preview_id, kind,
 *   baseline, impact}）；
 * - 一切错误原样透传（不重试、不改写）：422 range_invalid
 *   （details.rule：empty | out_of_bounds | proxy_split | blank |
 *   non_integer，message 含 canonical 长度、当前 range 与 split_point）、
 *   422 precondition_failed（details.reason：source_revision_inactive |
 *   already_retired）、422 validation_failed（details.violations：
 *   mismatch | reused | left_name/right_name required/max_length）、
 *   404 not_found、network。
 */
export function previewSplitFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: SplitPreviewParams,
): Promise<StudioApiResponse<SplitPreviewDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/split`;
  const body: Record<string, unknown> = {
    target_fragment_id: params.targetFragmentId,
    split_point: params.splitPoint,
  };
  if (params.leftName !== undefined) body.left_name = params.leftName;
  if (params.rightName !== undefined) body.right_name = params.rightName;
  return client.post<SplitPreviewDto>(path, body, {
    commandId: params.commandId,
  });
}

/** F9 拆分应用参数。 */
export interface SplitApplyParams {
  previewId: string;
  commandId: string;
  expectedRevision: number;
  expectedRangeSetRevision: number;
}

/**
 * F9 拆分应用响应（F9 冻结形状 {left, right, original}：left/right 各
 * object_ref(revision=1)+range，original object_ref(revision=current)
 * +state='retired'）。
 */
export interface SplitApplyDto {
  left: {
    object_ref: { kind: string; id: string; revision: number };
    range: { start: number; end: number };
  };
  right: {
    object_ref: { kind: string; id: string; revision: number };
    range: { start: number; end: number };
  };
  original: {
    object_ref: { kind: string; id: string; revision: number };
    state: string;
  };
}

/**
 * F9 应用片段拆分（附录 02 §2 F9；原片段退役不新建其 revision，左/右
 * 新片段各得一行 revision=1（reason='split'、predecessor=[原 fid]）、
 * state='candidate'；range_set 是 CAS 唯一写点之一，cas+1；幂等重放
 * 命中 → 200 原 result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/{fid}/split/apply，body =
 *   { preview_id, command_id, expected_revision, expected_range_set_revision }
 *   （command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res（SplitApplyDto：left/right 各
 *   object_ref(revision=1)+range、original object_ref(revision=current)
 *   +state='retired'）；
 * - 一切错误原样透传（不重试、不改写）：409 preview_stale
 *   （details.preview_id + details.conflicts）、409 revision_conflict
 *   （details.object.kind：fragment | range_set）、422 validation_failed
 *   （violations：mismatch | reused）、422 precondition_failed、
 *   404 not_found、network。
 */
export function applySplitFragment(
  client: StudioClient,
  pid: string,
  fid: string,
  params: SplitApplyParams,
): Promise<StudioApiResponse<SplitApplyDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/${encodeURIComponent(fid)}/split/apply`;
  return client.post<SplitApplyDto>(
    path,
    {
      preview_id: params.previewId,
      expected_revision: params.expectedRevision,
      expected_range_set_revision: params.expectedRangeSetRevision,
    },
    { commandId: params.commandId },
  );
}

// ---------------------------------------------------------------------------
// SOURCE-14 · 片段合并（契约 v2.9，附录 02 §2 F10）
// ---------------------------------------------------------------------------

/** F10 合并预览参数。 */
export interface MergePreviewParams {
  fragmentIds: string[];
  mergedName?: string;
  commandId: string;
}

/**
 * F10 合并预览响应（附录 00 §5.1：恰 4 字段，preview 是一次幂等写，
 * 记录 state=pending 不外露；命名派生/边界相接/合并范围重叠均在
 * preview 阶段即校验——F10 行冻结）。
 */
export interface MergePreviewDto {
  preview_id: string;
  kind: string;
  baseline: {
    fragment_a: { kind: string; id: string; revision: number };
    fragment_b: { kind: string; id: string; revision: number };
    range_set: { id: string | null; cas: number | null };
  };
  impact: {
    affected: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    needs_review: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
    preservable: Array<{
      object_ref: { kind: string; id: string; revision: number | null };
    }>;
  };
}

/**
 * F10 预览片段合并（附录 02 §2 F10；preview 是一次幂等写，记录
 * state=pending 不外露；幂等重放命中 → 200 原 result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/merge，body =
 *   { fragment_ids, command_id }（command_id 由 client.post 合并进 body）；
 *   merged_name 仅在非空时附加（params.mergedName !== undefined）；
 * - 成功（200，含幂等重放）返回 res（恰 4 字段 {preview_id, kind,
 *   baseline, impact}）；
 * - 一切错误原样透传（不重试、不改写）：422 validation_failed
 *   （violations：fragment_ids cardinality/duplicate、merged_name
 *   required/max_length、command_id rule=reused）、422 precondition_failed
 *   （details.reason：source_revision_inactive | already_retired |
 *   not_contiguous）、409 range_overlap（details.conflicts）、
 *   404 not_found、network。
 */
export function previewMergeFragments(
  client: StudioClient,
  pid: string,
  params: MergePreviewParams,
): Promise<StudioApiResponse<MergePreviewDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/merge`;
  const body: Record<string, unknown> = {
    fragment_ids: params.fragmentIds,
  };
  if (params.mergedName !== undefined) body.merged_name = params.mergedName;
  return client.post<MergePreviewDto>(path, body, {
    commandId: params.commandId,
  });
}

/** F10 合并应用参数。 */
export interface MergeApplyParams {
  previewId: string;
  commandId: string;
  expectedRevisionA: number;
  expectedRevisionB: number;
  expectedRangeSetRevision: number;
}

/**
 * F10 合并应用响应（F10 冻结形状恰 2 字段：merged object_ref(revision=1)
 * +range+name、predecessors 各 object_ref+state='retired'×2 请求序）。
 */
export interface MergeApplyDto {
  merged: {
    object_ref: { kind: string; id: string; revision: number };
    range: { start: number; end: number };
    name: string;
  };
  predecessors: Array<{
    object_ref: { kind: string; id: string; revision: number };
    state: string;
  }>;
}

/**
 * F10 应用片段合并（附录 02 §2 F10；a/b 退役不新建其 revision，merged 新
 * ID 得一行 revision=1（reason='merge'、predecessor=[a,b] 请求序）、
 * state='candidate'；range_set 是 CAS 唯一写点之一，cas+1；幂等重放命中
 * → 200 原 result_payload，零写零 commit）。
 *
 * - POST /api/studio/projects/{pid}/fragments/merge/apply，body =
 *   { preview_id, command_id, expected_revision_a, expected_revision_b,
 *   expected_range_set_revision }（command_id 由 client.post 合并进 body）；
 * - 成功（200，含幂等重放）返回 res（MergeApplyDto：merged
 *   object_ref(revision=1)+range+name、predecessors 各 object_ref+
 *   state='retired'）；
 * - 一切错误原样透传（不重试、不改写）：409 preview_stale
 *   （details.preview_id + details.conflicts）、409 revision_conflict
 *   （details.object.kind：fragment | range_set）、422 validation_failed
 *   （violations：mismatch | reused）、422 precondition_failed、
 *   404 not_found、network。
 */
export function applyMergeFragments(
  client: StudioClient,
  pid: string,
  params: MergeApplyParams,
): Promise<StudioApiResponse<MergeApplyDto>> {
  const path = `/api/studio/projects/${encodeURIComponent(pid)}/fragments/merge/apply`;
  return client.post<MergeApplyDto>(
    path,
    {
      preview_id: params.previewId,
      expected_revision_a: params.expectedRevisionA,
      expected_revision_b: params.expectedRevisionB,
      expected_range_set_revision: params.expectedRangeSetRevision,
    },
    { commandId: params.commandId },
  );
}
