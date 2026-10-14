// ---------------------------------------------------------------------------
// VIEW-03 · 统一选择与对象属性：纯逻辑
// （无 React / DOM / 网络 / 浏览器存储；转译后可被 vm 直接加载）
//
// 为什么另立宽松选择类型（主模型裁定，VIEW-01 契约一律不动）：
//   VIEW-01 冻结的 ObjectRef / normalizeSelectionRef 要求 revision 为正整数
//   （state.test.cjs 28 例锁死）。但真实对象里场（附录 04 §1：就地改名、无版本）、
//   剪辑实例等的 revision 可以是 null；用冻结的 normalizeSelectionRef 归一会把
//   整条选择丢掉。于是本模块新增 SelectionRef（revision: number | null）与
//   normalizeSelection——**不修改** studio-state.ts 的任何既有导出/语义。
//
// 一条选择的三个展示面（节点高亮 / 属性 / 出处）必须指向**同一对象同一 revision**：
//   · object_ref 原样携带入参 revision，绝不"升级"到来源的最新 revision；
//   · 只有 revision 与入参完全一致的来源数据才被采用；否则 available=false，
//     字段保守留空——拿别的 revision 的数据冒充才是编造事实。
//
// 归属 vs 展示分组：attribution 只来自对象自身的**真实父级外键**（如 shot.scene_id、
//   edit_instance.draft_id），绝不从展示用分组标题（group_title 之类）推导；
//   来源里只有标题而没有真实父级 id 时 attribution 为 null（不编造）。
//
// 资源与实例不混淆：画布节点保留其资源身份（fragment/asset/shot/scene…），
//   剪辑实例一律归到 kind="edit_instance"（实例行 id），实例引用的媒体资源
//   只算出处、永不成为实例的选择身份。
// ---------------------------------------------------------------------------

/** 宽松选择引用：revision 允许 null（无版本的真实对象，如场）。 */
export interface SelectionRef {
  kind: string;
  id: string;
  revision: number | null;
}

/** 属性视图里的单个字段（对象自身字段清单的一项）。 */
export interface ObjectField {
  key: string;
  label: string;
  value: string | number | boolean | null;
}

/** 版本/revision 信息；label/updated_at 未知即 null。 */
export interface ObjectVersionInfo {
  revision: number | null;
  label: string | null;
  updated_at: string | null;
}

/** 对象属性视图模型：高亮、属性、出处共用同一 object_ref（含 revision）。 */
export interface ObjectView {
  /** 被展示的真实 ref；revision 与入参选择一致。 */
  object_ref: SelectionRef;
  /** 展示类型名（来源缺失时退化为 kind 的已知中文名）。 */
  kind_label: string;
  /** 展示标题（来源缺失时为空串占位，不编造）。 */
  title: string;
  /** 归属：对象真正所属的父级（无真实父级 id 时为 null）。 */
  attribution: SelectionRef | null;
  /** 出处/来源链（真实来源对象，按由近及远排列；缺失时为空数组）。 */
  provenance: SelectionRef[];
  /** 版本信息（revision 恒等于 object_ref.revision）。 */
  version: ObjectVersionInfo;
  /** 状态（片段 confirmed/candidate/retired、资产 review_status 等；未知为 null）。 */
  state: string | null;
  /** 对象自身字段清单（缺失时为空数组）。 */
  fields: ObjectField[];
  /** 该 revision 的真实来源数据是否可用（false = 未加载/版本已变，字段保守留空）。 */
  available: boolean;
}

/** 来源数据里的一项字段。 */
export interface ObjectFieldInput {
  key: string;
  label?: string | null;
  value?: string | number | boolean | null;
}

/**
 * 真实对象数据（服务端 DTO 的投影）：属性视图的唯一输入。
 * 展示用分组标题（group_title 等）**不是**这里的字段——归属只认 parent。
 */
export interface ObjectSourceData {
  /** 真实对象身份（含 revision；无版本对象显式给 revision: null）。 */
  object_ref: SelectionRef;
  kind_label?: string | null;
  title?: string | null;
  /** 真实 DTO 常以 name 表达标题。 */
  name?: string | null;
  /** 真实父级外键（如 shot→scene、edit_instance→edit draft），无则 null。 */
  parent?: SelectionRef | null;
  /** 出处链（如 shot→script_adoption→fragment）。 */
  provenance?: readonly SelectionRef[] | null;
  version_label?: string | null;
  updated_at?: string | null;
  state?: string | null;
  fields?: readonly ObjectFieldInput[] | null;
}

/** 剪辑实例的输入投影（媒体资源只作出处，不作选择身份）。 */
export interface EditInstanceInput {
  /** 实例自身 ref（仅 kind="edit_instance" 时被采信）。 */
  object_ref?: SelectionRef | null;
  /** 实例行 id（studio_edit_instances.id）。 */
  id?: string | null;
  instance_id?: string | null;
  revision?: number | null;
  edit_revision?: number | null;
  /** 引用的媒体/资产资源：只算出处，绝不作为实例选择身份。 */
  media_id?: string | null;
}

/** 剪辑实例的 kind（资源与实例选择的区分锚点）。 */
export const EDIT_INSTANCE_KIND = "edit_instance";

/** 已知 kind 的展示名（未知 kind 退化为 kind 原文，不编造业务含义）。 */
const KIND_LABELS: Readonly<Record<string, string>> = {
  project: "项目",
  source_revision: "来源版本",
  range_set: "范围集合",
  fragment: "故事片段",
  script_object: "剧本对象",
  script_adoption: "剧本采用",
  scene: "场",
  shot: "镜头",
  asset: "资产",
  source_relation: "出处关系",
  asset_occurrence: "原文出现",
  asset_binding: "生成输入绑定",
  change_preview: "变更预览",
  review_decision: "审核决定",
  edit: "剪辑",
  edit_instance: "剪辑实例",
  confirmed_edit: "确认剪辑",
  media_artifact: "媒体产物",
  release: "发布",
  job: "任务",
  job_attempt: "任务尝试",
  job_event: "任务事件",
  conversation: "对话",
  message: "消息",
  proposal: "提案",
};

/** kind 的展示名：已知取中文名，未知原样返回 kind（不编造）。 */
export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isPositiveIntegerOrNull(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isInteger(value) && value >= 1);
}

function positiveOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? value : null;
}

/**
 * 归一化宽松选择：kind/id 非空字符串；revision 允许 null，
 * 非 null 时必须是正整数，否则**整条**归 null（不抛错、不保留脏值）。
 */
export function normalizeSelection(ref: SelectionRef | null): SelectionRef | null {
  if (ref === null || typeof ref !== "object") return null;
  const candidate = ref as SelectionRef;
  if (!isNonEmptyString(candidate.kind) || !isNonEmptyString(candidate.id)) return null;
  if (!isPositiveIntegerOrNull(candidate.revision)) return null;
  return { kind: candidate.kind, id: candidate.id, revision: candidate.revision };
}

/** 形状判定（选择门禁）：合法 SelectionRef 形状即可选中（revision: null 合法）。 */
export function isSelectableObjectRef(value: unknown): value is SelectionRef {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  return normalizeSelection(value as SelectionRef) !== null;
}

/**
 * 关系引用（父级/出处）归一：kind/id 非空必填；revision 缺省或为 null → null，
 * 出现但非法（0/-1/1.5/"1"）时整条关系不采用（不编造版本）。
 */
function normalizeRelationRef(value: unknown): SelectionRef | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as { kind?: unknown; id?: unknown; revision?: unknown };
  if (!isNonEmptyString(candidate.kind) || !isNonEmptyString(candidate.id)) return null;
  if (candidate.revision === undefined || candidate.revision === null) {
    return { kind: candidate.kind, id: candidate.id, revision: null };
  }
  const revision = positiveOrNull(candidate.revision);
  if (revision === null) return null;
  return { kind: candidate.kind, id: candidate.id, revision };
}

// ── 对象身份（与 canvas-nodes.ts 的 nodeKey 同构：kind + id，不含 revision） ──

/** 对象身份键 kind"\0"id；无法构成身份时返回 null。 */
export function objectKey(ref: SelectionRef | null | undefined): string | null {
  if (ref === null || ref === undefined || typeof ref !== "object") return null;
  if (!isNonEmptyString(ref.kind) || !isNonEmptyString(ref.id)) return null;
  return `${ref.kind}\u0000${ref.id}`;
}

/** 两个选择是否指向同一对象（身份相同，忽略 revision）。 */
export function sameObject(
  a: SelectionRef | null | undefined,
  b: SelectionRef | null | undefined,
): boolean {
  const keyA = objectKey(a);
  return keyA !== null && keyA === objectKey(b);
}

/** 两个选择是否指向同一对象**且同一 revision**（高亮/属性/出处一致性的判据）。 */
export function sameObjectRevision(
  a: SelectionRef | null | undefined,
  b: SelectionRef | null | undefined,
): boolean {
  if (!sameObject(a, b) || a === null || a === undefined || b === null || b === undefined) {
    return false;
  }
  return a.revision === b.revision;
}

// ── 画布节点 / 剪辑实例 → 选择 ─────────────────────────────────────────────

/**
 * 画布节点 → 选择：保留节点的真实对象身份（资源就是资源）。
 * 没有 object_ref（拿不到真实身份）时返回 null，不凭展示 kind 伪造选择。
 */
export function nodeToSelection(
  node: { object_ref?: SelectionRef | null } | null | undefined,
): SelectionRef | null {
  if (node === null || node === undefined || typeof node !== "object") return null;
  return normalizeSelection(node.object_ref ?? null);
}

/**
 * 剪辑实例 → 选择：kind 恒为 edit_instance，id 取实例行 id。
 * 实例引用的媒体/资产资源只算出处，绝不成为选择身份（资源与实例不混淆）。
 */
export function instanceToSelection(
  instance: EditInstanceInput | null | undefined,
): SelectionRef | null {
  if (instance === null || instance === undefined || typeof instance !== "object") return null;

  // 实例自身 ref 只在明确是 edit_instance 时采信；否则退回实例行 id，
  // 绝不把媒体资源 ref 当成实例选择。
  const own = normalizeSelection(instance.object_ref ?? null);
  if (own !== null && own.kind === EDIT_INSTANCE_KIND) return own;

  const id = isNonEmptyString(instance.instance_id)
    ? instance.instance_id
    : isNonEmptyString(instance.id)
      ? instance.id
      : null;
  if (id === null) return null;
  const revision =
    positiveOrNull(instance.revision) ?? positiveOrNull(instance.edit_revision) ?? null;
  return { kind: EDIT_INSTANCE_KIND, id, revision };
}

// ── 派生属性视图 ───────────────────────────────────────────────────────────

function findSource(
  sources: readonly ObjectSourceData[] | null | undefined,
  ref: SelectionRef,
): ObjectSourceData | null {
  if (!Array.isArray(sources)) return null;
  for (const source of sources) {
    if (source === null || source === undefined || typeof source !== "object") continue;
    const identity = normalizeSelection(source.object_ref ?? null);
    if (identity === null) continue;
    if (identity.kind === ref.kind && identity.id === ref.id) return source;
  }
  return null;
}

function sourceRevision(source: ObjectSourceData): number | null {
  const identity = normalizeSelection(source.object_ref ?? null);
  return identity === null ? null : identity.revision;
}

function normalizeFields(input: unknown): ObjectField[] {
  if (!Array.isArray(input)) return [];
  const fields: ObjectField[] = [];
  for (const raw of input) {
    if (raw === null || raw === undefined || typeof raw !== "object") continue;
    const entry = raw as ObjectFieldInput;
    if (!isNonEmptyString(entry.key)) continue;
    fields.push({
      key: entry.key,
      label: isNonEmptyString(entry.label) ? entry.label : entry.key,
      value: normalizeFieldValue(entry.value),
    });
  }
  return fields;
}

function normalizeFieldValue(value: unknown): string | number | boolean | null {
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  return null;
}

function normalizeProvenance(input: unknown): SelectionRef[] {
  if (!Array.isArray(input)) return [];
  const chain: SelectionRef[] = [];
  for (const raw of input) {
    const ref = normalizeRelationRef(raw);
    if (ref !== null) chain.push(ref);
  }
  return chain;
}

/**
 * 由真实对象数据派生属性视图：
 *   · 空白/非法选择 → null（清空，不抛错）；
 *   · object_ref.revision 恒等于入参 revision（不升级到来源最新版）；
 *   · 只有 revision（含同为 null）与入参一致的来源数据才被采用，否则
 *     available=false 且字段/归属/出处/状态保守留空；
 *   · attribution 只来自来源的真实父级字段，绝不使用分组标题。
 */
export function deriveObjectView(
  objectRef: SelectionRef | null,
  sources: readonly ObjectSourceData[] | null | undefined,
): ObjectView | null {
  const requested = normalizeSelection(objectRef);
  if (requested === null) return null;

  const matched = findSource(sources, requested);
  // revision 不一致 = 这份数据描述的是别的 revision，不能拿来冒充。
  const source =
    matched !== null && sourceRevision(matched) === requested.revision ? matched : null;

  const attribution = source === null ? null : normalizeRelationRef(source.parent ?? null);
  const provenance = source === null ? [] : normalizeProvenance(source.provenance ?? null);
  const fields = source === null ? [] : normalizeFields(source.fields ?? null);

  const title =
    source === null
      ? ""
      : isNonEmptyString(source.title)
        ? source.title
        : isNonEmptyString(source.name)
          ? source.name
          : "";

  return {
    object_ref: requested,
    kind_label:
      source !== null && isNonEmptyString(source.kind_label)
        ? source.kind_label
        : kindLabel(requested.kind),
    title,
    attribution,
    provenance,
    version: {
      revision: requested.revision,
      label: source !== null && isNonEmptyString(source.version_label) ? source.version_label : null,
      updated_at: source !== null && isNonEmptyString(source.updated_at) ? source.updated_at : null,
    },
    state: source !== null && isNonEmptyString(source.state) ? source.state : null,
    fields,
    available: source !== null,
  };
}
