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
