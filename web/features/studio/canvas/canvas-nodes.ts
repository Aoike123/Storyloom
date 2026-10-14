// ---------------------------------------------------------------------------
// VIEW-02 · 受控无限画布：纯逻辑（无 React / DOM / 网络依赖，可被 vm 直接加载）
//
// 冻结契约（本模块是画布的唯一布局真相来源，组件只是它的受控视图）：
//   ① 节点 = 真实来源对象（fragment/asset/shot/scene …）的投影，不伪造样例数据：
//      没有来源对象就没有节点；来源新增一条，派生结果立即多一个节点（无缓存）。
//   ② 坐标（x/y）只由「布局状态」CanvasLayout（按对象身份键控）提供，
//      绝不由来源对象在数组中的下标/顺序推导；没有布局记录时落 DEFAULT_POSITION。
//   ③ 派生与更新一律纯函数：不原地修改入参、不改来源对象、不改来源顺序、
//      不触碰 revision/采用信息（revision 原样透传）。
//   ④ 视口（pan/zoom/fit）为可预测的最小数学：非法输入不产生 NaN，
//      一律返回原视口引用（或 null，见 contentToScreen），语义在测试中固定。
//
// ObjectRef 复用 shell/studio-state.ts 的冻结形状（附录 00：{kind, id, revision}）。
// 这里只做 type-only 引用：转译后无运行时 import，vm 可直接执行。
// ---------------------------------------------------------------------------

import type { ObjectRef } from "../shell/studio-state";

export type { ObjectRef };

/** 画布节点：真实对象的投影 + 自由布局坐标。 */
export interface CanvasNode {
  /** 来源对象身份（ObjectRef 冻结形状；revision 随来源同步，不参与坐标键）。 */
  object_ref: ObjectRef;
  /** 展示类型（来源未给时取 object_ref.kind）。 */
  kind: string;
  /** 展示标签（来源未给时为空串）。 */
  label: string;
  /** 自由布局坐标：只来自布局状态，与来源顺序无关。 */
  x: number;
  y: number;
}

/** 来源对象投影：节点派生的唯一输入（真实 ref + 展示字段）。 */
export interface CanvasSourceObject {
  object_ref: ObjectRef;
  kind?: string;
  label?: string | null;
}

/** 单个节点在布局状态中的位置。 */
export interface NodePosition {
  x: number;
  y: number;
}

/** 布局状态：按「对象身份」（kind + id，不含 revision）键控的坐标表。 */
export type CanvasLayout = Readonly<Record<string, NodePosition>>;

/** 无布局记录时的保守落点：不按列表下标排布，一律落原点。 */
export const DEFAULT_POSITION: NodePosition = { x: 0, y: 0 };

/** 节点的视觉尺寸（内容坐标单位；fitToNodes 与组件 CSS 共用同一常量）。 */
export const NODE_WIDTH = 168;
export const NODE_HEIGHT = 72;

/** fit 时内容四周留白（内容坐标单位）。 */
export const FIT_PADDING = 48;

/** 视口缩放上下限（含端点）。 */
export const MIN_ZOOM = 0.1;
export const MAX_ZOOM = 8;

/** 视口：屏幕坐标 = 内容坐标 × zoom + (x, y)；缩放围绕内容原点 (0,0) 展开。 */
export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  width: number;
  height: number;
}

// ── 对象身份 ───────────────────────────────────────────────────────────────

/**
 * 节点键：由 object_ref 的 kind + id 构成，**不含 revision**
 * （对象升版不得重置已持久化坐标）。无法构成身份时返回 null。
 */
export function nodeKey(ref: ObjectRef | null | undefined): string | null {
  if (ref === null || ref === undefined) return null;
  if (typeof ref.kind !== "string" || ref.kind.length === 0) return null;
  if (typeof ref.id !== "string" || ref.id.length === 0) return null;
  return `${ref.kind}\u0000${ref.id}`;
}

/** 校验并复制 ref：kind/id 非空字符串且 revision 为有限数字才有效（revision 原样透传）。 */
function normalizeRef(ref: ObjectRef | null | undefined): ObjectRef | null {
  const key = nodeKey(ref);
  if (key === null || ref === null || ref === undefined) return null;
  if (typeof ref.revision !== "number" || !Number.isFinite(ref.revision)) {
    return null;
  }
  return { kind: ref.kind, id: ref.id, revision: ref.revision };
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isPosition(value: unknown): value is NodePosition {
  return (
    typeof value === "object" &&
    value !== null &&
    isFiniteNumber((value as NodePosition).x) &&
    isFiniteNumber((value as NodePosition).y)
  );
}

/** 读布局坐标：命中且合法才采用，否则落 DEFAULT_POSITION（每次返回新对象）。 */
function readPosition(layout: CanvasLayout | null | undefined, key: string | null): NodePosition {
  if (layout !== null && layout !== undefined && key !== null) {
    const pos = layout[key];
    if (isPosition(pos)) return { x: pos.x, y: pos.y };
  }
  return { x: DEFAULT_POSITION.x, y: DEFAULT_POSITION.y };
}

function displayKind(source: CanvasSourceObject, ref: ObjectRef): string {
  return typeof source.kind === "string" && source.kind.length > 0 ? source.kind : ref.kind;
}

function displayLabel(source: CanvasSourceObject): string {
  return typeof source.label === "string" ? source.label : "";
}

// ── 派生：来源对象 + 布局 → 节点（纯函数，无缓存） ────────────────────────

/**
 * 从真实来源对象派生节点：
 *   · 空输入 → 空数组（不伪造样例数据）；
 *   · 同输入同输出、返回全新对象（不与入参、不与上一次调用共享引用）；
 *   · 异步读入的新对象一旦进入 sources 就立即出现（无缓存陈旧）；
 *   · display 字段（kind/label/object_ref.revision）每次随来源同步；
 *   · x/y 只来自 layout，来源顺序/下标不参与坐标推导。
 * 身份非法（缺 kind/id 或 revision 非有限数字）的来源被跳过。
 */
export function deriveNodes(
  sources: readonly CanvasSourceObject[] | null | undefined,
  layout: CanvasLayout | null | undefined,
): CanvasNode[] {
  if (!Array.isArray(sources) || sources.length === 0) return [];
  const nodes: CanvasNode[] = [];
  for (const source of sources) {
    if (source === null || source === undefined) continue;
    const ref = normalizeRef(source.object_ref);
    if (ref === null) continue;
    const pos = readPosition(layout, nodeKey(ref));
    nodes.push({
      object_ref: ref,
      kind: displayKind(source, ref),
      label: displayLabel(source),
      x: pos.x,
      y: pos.y,
    });
  }
  return nodes;
}

// ── 受控增删改（全部纯函数，返回新数组/新对象，不原地改） ─────────────────

/**
 * 按 object_ref 新增或更新单个节点（返回新数组）：
 *   · 无 → 追加，坐标取 position（非法/缺省则 DEFAULT_POSITION）；
 *   · 有 → 同步展示字段与 revision，**保留既有 x/y**（含显式传入 position 时不覆盖）；
 *   · 身份非法 → 原数组原样返回（不伪造节点）。
 */
export function upsertNode(
  nodes: readonly CanvasNode[],
  source: CanvasSourceObject | null | undefined,
  position?: NodePosition,
): CanvasNode[] {
  const list = Array.isArray(nodes) ? nodes : [];
  if (source === null || source === undefined) return list as CanvasNode[];
  const ref = normalizeRef(source.object_ref);
  const key = nodeKey(ref);
  if (ref === null || key === null) return list as CanvasNode[];

  const kind = displayKind(source, ref);
  const label = displayLabel(source);
  const index = list.findIndex((node) => nodeKey(node.object_ref) === key);

  if (index === -1) {
    const pos = isPosition(position) ? { x: position.x, y: position.y } : readPosition(null, null);
    return [...list, { object_ref: ref, kind, label, x: pos.x, y: pos.y }];
  }

  const next = list.slice();
  next[index] = { ...list[index], object_ref: ref, kind, label };
  return next;
}

/** 按 object_ref 删除节点：不存在时返回原数组引用（无变化即无新对象）。 */
export function removeNode(nodes: readonly CanvasNode[], ref: ObjectRef | null | undefined): CanvasNode[] {
  const list = Array.isArray(nodes) ? nodes : [];
  const key = nodeKey(ref);
  if (key === null) return list as CanvasNode[];
  const index = list.findIndex((node) => nodeKey(node.object_ref) === key);
  if (index === -1) return list as CanvasNode[];
  return list.filter((_node, i) => i !== index);
}

/**
 * 更新某节点坐标（返回新数组）：只替换命中节点，其余节点保持原引用；
 * 未命中或坐标非法时返回原数组引用（不产生 NaN）；输入数组不被修改。
 */
export function updateNodePosition(
  nodes: readonly CanvasNode[],
  ref: ObjectRef | null | undefined,
  x: number,
  y: number,
): CanvasNode[] {
  const list = Array.isArray(nodes) ? nodes : [];
  const key = nodeKey(ref);
  if (key === null || !isFiniteNumber(x) || !isFiniteNumber(y)) {
    return list as CanvasNode[];
  }
  let changed = false;
  const next = list.map((node) => {
    if (nodeKey(node.object_ref) !== key) return node;
    if (node.x === x && node.y === y) return node;
    changed = true;
    return { ...node, x, y };
  });
  return changed ? next : (list as CanvasNode[]);
}

/** 写入布局坐标（返回新布局）：非法入参返回原布局引用；原布局不被修改。 */
export function updateLayoutPosition(
  layout: CanvasLayout | null | undefined,
  ref: ObjectRef | null | undefined,
  x: number,
  y: number,
): CanvasLayout {
  const base = layout === null || layout === undefined ? {} : layout;
  const key = nodeKey(ref);
  if (key === null || !isFiniteNumber(x) || !isFiniteNumber(y)) return base;
  return { ...base, [key]: { x, y } };
}

/** 删除布局坐标（返回新布局）：键不存在时返回原布局引用。 */
export function removeLayoutPosition(
  layout: CanvasLayout | null | undefined,
  ref: ObjectRef | null | undefined,
): CanvasLayout {
  const base = layout === null || layout === undefined ? {} : layout;
  const key = nodeKey(ref);
  if (key === null || !Object.prototype.hasOwnProperty.call(base, key)) return base;
  const next: Record<string, NodePosition> = {};
  for (const k of Object.keys(base)) {
    if (k !== key) next[k] = base[k];
  }
  return next;
}

// ── 视口数学（pan / zoom / fit） ───────────────────────────────────────────

export function identityViewport(): Viewport {
  return { x: 0, y: 0, zoom: 1 };
}

function isValidViewport(viewport: Viewport | null | undefined): viewport is Viewport {
  return (
    viewport !== null &&
    viewport !== undefined &&
    isFiniteNumber(viewport.x) &&
    isFiniteNumber(viewport.y) &&
    isFiniteNumber(viewport.zoom) &&
    viewport.zoom > 0
  );
}

function isValidPoint(point: Point | null | undefined): point is Point {
  return point !== null && point !== undefined && isFiniteNumber(point.x) && isFiniteNumber(point.y);
}

function isValidSize(size: Size | null | undefined): size is Size {
  return (
    size !== null &&
    size !== undefined &&
    isFiniteNumber(size.width) &&
    isFiniteNumber(size.height) &&
    size.width > 0 &&
    size.height > 0
  );
}

function clampZoom(zoom: number): number {
  if (zoom < MIN_ZOOM) return MIN_ZOOM;
  if (zoom > MAX_ZOOM) return MAX_ZOOM;
  return zoom;
}

/** 内容坐标 → 屏幕坐标；视口或点非法时返回 null。 */
export function contentToScreen(viewport: Viewport, point: Point): Point | null {
  if (!isValidViewport(viewport) || !isValidPoint(point)) return null;
  return { x: point.x * viewport.zoom + viewport.x, y: point.y * viewport.zoom + viewport.y };
}

/** 屏幕坐标 → 内容坐标；视口或点非法时返回 null。 */
export function screenToContent(viewport: Viewport, point: Point): Point | null {
  if (!isValidViewport(viewport) || !isValidPoint(point)) return null;
  return { x: (point.x - viewport.x) / viewport.zoom, y: (point.y - viewport.y) / viewport.zoom };
}

/** 平移视口（屏幕像素）；入参非法时返回原视口引用，不产生 NaN。 */
export function panBy(viewport: Viewport, dx: number, dy: number): Viewport {
  if (!isValidViewport(viewport) || !isFiniteNumber(dx) || !isFiniteNumber(dy)) {
    return viewport;
  }
  return { x: viewport.x + dx, y: viewport.y + dy, zoom: viewport.zoom };
}

/**
 * 围绕屏幕锚点缩放：锚点下的内容坐标保持不动（不漂移）。
 * factor 必须为有限正数、anchor 必须为有限点，否则返回原视口引用；
 * 结果 zoom 被夹在 [MIN_ZOOM, MAX_ZOOM]，夹取后锚点依然不漂移。
 */
export function zoomAt(viewport: Viewport, factor: number, anchor: Point): Viewport {
  if (!isValidViewport(viewport) || !isFiniteNumber(factor) || factor <= 0 || !isValidPoint(anchor)) {
    return viewport;
  }
  const zoom = clampZoom(viewport.zoom * factor);
  const ratio = zoom / viewport.zoom;
  return {
    x: anchor.x - (anchor.x - viewport.x) * ratio,
    y: anchor.y - (anchor.y - viewport.y) * ratio,
    zoom,
  };
}

/**
 * 适配：把全部节点（含 NODE_WIDTH/NODE_HEIGHT 与 padding 留白）装进 size 并居中。
 * 空节点、非法 size/padding、坐标全非法的节点集 → 返回原视口引用。
 */
export function fitToNodes(
  viewport: Viewport,
  nodes: readonly CanvasNode[] | null | undefined,
  size: Size,
  padding: number = FIT_PADDING,
): Viewport {
  if (
    !isValidViewport(viewport) ||
    !isValidSize(size) ||
    !isFiniteNumber(padding) ||
    padding < 0 ||
    !Array.isArray(nodes) ||
    nodes.length === 0
  ) {
    return viewport;
  }

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    if (node === null || node === undefined || !isFiniteNumber(node.x) || !isFiniteNumber(node.y)) {
      continue;
    }
    if (node.x < minX) minX = node.x;
    if (node.y < minY) minY = node.y;
    if (node.x + NODE_WIDTH > maxX) maxX = node.x + NODE_WIDTH;
    if (node.y + NODE_HEIGHT > maxY) maxY = node.y + NODE_HEIGHT;
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return viewport;

  const contentWidth = maxX - minX + padding * 2;
  const contentHeight = maxY - minY + padding * 2;
  const zoom = clampZoom(Math.min(size.width / contentWidth, size.height / contentHeight));
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  return {
    x: size.width / 2 - centerX * zoom,
    y: size.height / 2 - centerY * zoom,
    zoom,
  };
}
