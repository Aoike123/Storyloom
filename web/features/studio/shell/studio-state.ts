// ---------------------------------------------------------------------------
// VIEW-01 · 新项目工作台状态容器（纯 TS，无 React 依赖；React 绑定在 StudioApp.tsx）
//
// 状态五职责：
//   ① project      当前项目 DTO 摘要 + projectId
//   ② selection    ObjectRef 单选（fragment/asset/shot/scene/edit 等）
//   ③ view         活动视图/标签（R02 五种工作区职责 + 对话）
//   ④ sourceSwitch 来源区开关（原文 / 故事片段）+ active_source_revision_id
//   ⑤ jobFeed      后台 job 状态条（仅展示，永不驱动视图切换）
//
// 项目隔离：单槽 + 切换清空（setProject / switchProject / hydrate 均把
// selection/view/jobFeed 重置到新项目初态，旧项目状态不残留、不串）。
// 业务模型不进本 reducer：只存 id/ref/DTO 摘要；片段文本编辑、表单态由
// 各域组件自管。恢复一律经 hydrate 从服务端数据重建，不读 localStorage。
// ---------------------------------------------------------------------------

/** ObjectRef（附录 00 冻结形状：{kind, id, revision}）。 */
export interface ObjectRef {
  kind: string;
  id: string;
  revision: number;
}

/**
 * 项目 DTO 摘要：容器只取所需字段（结构兼容 source/api.ts 的 ProjectDto，
 * 完整 DTO 可直接赋值进来）。
 */
export interface ProjectDtoSummary {
  id: string;
  name: string;
  active_source_revision_id: string | null;
}

/** 活动视图枚举：R02 主工作区五种职责 + 对话（R10）。 */
export type StudioView =
  | "source"
  | "script"
  | "storyboard"
  | "assets"
  | "film"
  | "chat";

/** 来源区开关（R03：来源区两种视图）。 */
export type SourceSwitchMode = "reader" | "fragments";

/** 后台 job 状态（R08 真实记录状态）。 */
export type JobStatus =
  | "queued"
  | "running"
  | "awaiting_review"
  | "completed"
  | "failed"
  | "cancelled"
  | "superseded";

/** job 状态条条目：仅展示摘要，不含业务负载。 */
export interface JobFeedEntry {
  id: string;
  status: JobStatus;
  label: string | null;
}

export interface StudioState {
  project: {
    project: ProjectDtoSummary | null;
    projectId: string | null;
  };
  selection: {
    ref: ObjectRef | null;
  };
  view: {
    current: StudioView;
  };
  sourceSwitch: {
    mode: SourceSwitchMode;
    activeSourceRevisionId: string | null;
  };
  jobFeed: {
    entries: JobFeedEntry[];
  };
}

export type StudioAction =
  /** 项目切换（仅 id）：重置 selection/view/jobFeed 到新项目初态。 */
  | { type: "setProject"; projectId: string | null }
  /** 项目切换（带 DTO 摘要）：重置并载入新项目 DTO。 */
  | { type: "switchProject"; project: ProjectDtoSummary }
  /**
   * 项目重开/刷新：从服务端数据重建状态（片段恢复正确）；
   * 不从 localStorage 恢复业务数据。
   */
  | { type: "hydrate"; project: ProjectDtoSummary; selectionRef: ObjectRef | null }
  /** 单选选择（null 清空）。 */
  | { type: "setSelection"; ref: ObjectRef | null }
  /** 切换活动视图。 */
  | { type: "setView"; view: StudioView }
  /** 来源区开关（原文 / 故事片段）。 */
  | { type: "setSourceSwitch"; mode: SourceSwitchMode }
  /** 更新 active_source_revision_id（来源开关态跟随）。 */
  | { type: "setActiveSourceRevision"; revisionId: string | null }
  /** 追加一条 job 状态（仅 jobFeed，永不改 view）。 */
  | { type: "pushJob"; job: JobFeedEntry }
  /** 更新一条 job 状态（仅 jobFeed，永不改 view）。 */
  | { type: "updateJob"; jobId: string; status: JobStatus }
  /** 清空 job 状态条。 */
  | { type: "clearJobFeed" };

/** 默认活动视图：来源（R02 左侧来源区贯穿工作台）。 */
export const DEFAULT_VIEW: StudioView = "source";

/** 默认来源区开关：原文视图。 */
export const DEFAULT_SOURCE_SWITCH_MODE: SourceSwitchMode = "reader";

/** job 状态条保留最近 N 条（仅展示）。 */
const JOB_FEED_CAP = 10;

/** 新项目初态（项目隔离的基准）。 */
export function createInitialState(): StudioState {
  return {
    project: { project: null, projectId: null },
    selection: { ref: null },
    view: { current: DEFAULT_VIEW },
    sourceSwitch: {
      mode: DEFAULT_SOURCE_SWITCH_MODE,
      activeSourceRevisionId: null,
    },
    jobFeed: { entries: [] },
  };
}

/** 校验并归一化 ObjectRef：无效（空 kind/id、revision 非正整数）归为 null。 */
export function normalizeSelectionRef(
  ref: ObjectRef | null,
): ObjectRef | null {
  if (ref === null) {
    return null;
  }
  if (
    typeof ref.kind === "string" &&
    ref.kind.length > 0 &&
    typeof ref.id === "string" &&
    ref.id.length > 0 &&
    Number.isInteger(ref.revision) &&
    ref.revision >= 1
  ) {
    return { kind: ref.kind, id: ref.id, revision: ref.revision };
  }
  return null;
}

export function studioReducer(
  state: StudioState,
  action: StudioAction,
): StudioState {
  switch (action.type) {
    case "setProject": {
      // 项目隔离：任何项目切换（含清空为 null）都重置
      // selection/view/jobFeed，旧项目状态不残留、不串。
      const next = createInitialState();
      next.project = { project: null, projectId: action.projectId };
      return next;
    }

    case "switchProject": {
      // 项目隔离 + 载入新项目 DTO 摘要。
      const next = createInitialState();
      next.project = {
        project: action.project,
        projectId: action.project.id,
      };
      next.sourceSwitch = {
        ...next.sourceSwitch,
        activeSourceRevisionId: action.project.active_source_revision_id,
      };
      return next;
    }

    case "hydrate": {
      // 项目重开/刷新：从服务端数据重建；选择指向的 ObjectRef
      // （含 revision）以服务端 DTO 为准。
      const next = createInitialState();
      next.project = {
        project: action.project,
        projectId: action.project.id,
      };
      next.selection = { ref: normalizeSelectionRef(action.selectionRef) };
      next.sourceSwitch = {
        ...next.sourceSwitch,
        activeSourceRevisionId: action.project.active_source_revision_id,
      };
      return next;
    }

    case "setSelection":
      return {
        ...state,
        selection: { ref: normalizeSelectionRef(action.ref) },
      };

    case "setView":
      return { ...state, view: { current: action.view } };

    case "setSourceSwitch":
      return {
        ...state,
        sourceSwitch: { ...state.sourceSwitch, mode: action.mode },
      };

    case "setActiveSourceRevision":
      return {
        ...state,
        sourceSwitch: {
          ...state.sourceSwitch,
          activeSourceRevisionId: action.revisionId,
        },
      };

    case "pushJob": {
      // 后台 job 状态只进 jobFeed，永不修改 view（不跳标签）。
      const entries = [...state.jobFeed.entries, action.job];
      return {
        ...state,
        jobFeed: { entries: entries.slice(-JOB_FEED_CAP) },
      };
    }

    case "updateJob": {
      const entries = state.jobFeed.entries.map((entry) =>
        entry.id === action.jobId
          ? { ...entry, status: action.status }
          : entry,
      );
      return { ...state, jobFeed: { entries } };
    }

    case "clearJobFeed":
      return { ...state, jobFeed: { entries: [] } };

    default:
      // 未知 action 类型：状态不变（返回原引用）。
      return state;
  }
}
