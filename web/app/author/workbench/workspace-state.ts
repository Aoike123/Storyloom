// 工作台框架：视图状态契约。
// 本轮（以交互稿为准）：五个工作标签（切分/剧本/分镜/资产/成片）+ 项目级左侧来源区；
// 只管理前端视图状态，业务状态（归属/顺序/素材绑定/确认/正文版本）由服务端权威，绝不在这里。
import { type SourceFragment, type SourceView } from "./source-model";

// 顶部工作标签。数量与名称为当前工作模型（暂定，D01 继续设计），不承载后端 stage 语义，不自动跳转。
export type Workspace = "cut" | "script" | "board" | "assets" | "film";

export const WORKSPACES: readonly Workspace[] = [
  "cut",
  "script",
  "board",
  "assets",
  "film",
];

export const WORKSPACE_LABELS: Record<Workspace, string> = {
  cut: "切分／裁减",
  script: "剧本",
  board: "分镜",
  assets: "资产",
  film: "成片",
};

export interface Viewport {
  x: number;
  y: number;
  scale: number;
}

export interface WorkspaceView {
  viewport: Viewport;
  selectedId: string | null;
  layout?: Record<string, { x: number; y: number }>;
}

// 片段跳转时记忆的目标工作上下文：进入该片段时恢复其 task + 选中对象（交互稿 contexts[]）。
export interface FragmentContext {
  task: Workspace;
  selected: string | null;
}

export interface WorkbenchState {
  projectId: string;
  activeWorkspace: Workspace;
  byWorkspace: Record<Workspace, WorkspaceView>;
  // 左侧来源区（项目级，跨工作区共享）
  sourceOpen: boolean;
  sourceView: SourceView;
  activeFragmentId: string | null; // 当前片段（原文/片段视图共用同一 id）
  pendingRange: { start: number; end: number } | null; // A2：划选未标注文字得到的候选区间
  candidateFragments: SourceFragment[]; // A2：前端创建、未持久化的候选片段
  contexts: Record<string, FragmentContext>; // 每片段记忆的工作上下文
  inspectorOpen: boolean; // 右侧属性面板（全局开关，随所选对象更新）
}

export type WorkbenchAction =
  | { type: "SET_PROJECT_ID"; id: string }
  | { type: "SET_WORKSPACE"; workspace: Workspace } // 仅用户主动切换
  | { type: "SET_INITIAL_WORKSPACE"; workspace: Workspace } // 加载项目时设定起始标签（空项目落“切分”），非用户切换
  | {
      type: "JUMP_FRAGMENT";
      id: string;
      task: Workspace;
      selected: string | null;
    } // 片段跳转
  | { type: "SELECT"; id: string | null }
  | { type: "SET_VIEWPORT"; viewport: Viewport }
  | { type: "SET_LAYOUT"; id: string; x: number; y: number }
  | { type: "SET_SOURCE_VIEW"; view: SourceView }
  | { type: "SET_SOURCE_OPEN"; open: boolean }
  | { type: "SET_PENDING_RANGE"; range: { start: number; end: number } | null }
  | { type: "ADD_CANDIDATE_FRAGMENT"; fragment: SourceFragment }
  | { type: "REMOVE_CANDIDATE_FRAGMENT"; id: string }
  | { type: "RENAME_CANDIDATE_FRAGMENT"; id: string; name: string }
  | { type: "SET_INSPECTOR"; open: boolean };

const DEFAULT_VIEWPORT: Viewport = { x: 0, y: 0, scale: 1 };

function emptyWorkspaceView(): WorkspaceView {
  return { viewport: { ...DEFAULT_VIEWPORT }, selectedId: null };
}

function emptyWorkspaces(): Record<Workspace, WorkspaceView> {
  return {
    cut: emptyWorkspaceView(),
    script: emptyWorkspaceView(),
    board: emptyWorkspaceView(),
    assets: emptyWorkspaceView(),
    film: emptyWorkspaceView(),
  };
}

export function createWorkbenchState(projectId: string): WorkbenchState {
  return {
    projectId,
    activeWorkspace: "board", // 制作类默认落在“分镜”（项目主体）
    byWorkspace: emptyWorkspaces(),
    sourceOpen: true,
    sourceView: "original",
    activeFragmentId: null,
    pendingRange: null,
    candidateFragments: [],
    contexts: {},
    inspectorOpen: true,
  };
}

// 只改“当前查看的工作区”的视图；其余工作区互不影响（按 项目 × 工作区 隔离）。
function updateActive(
  state: WorkbenchState,
  fn: (v: WorkspaceView) => WorkspaceView,
): WorkbenchState {
  const key = state.activeWorkspace;
  const next = fn(state.byWorkspace[key]);
  if (next === state.byWorkspace[key]) return state;
  return { ...state, byWorkspace: { ...state.byWorkspace, [key]: next } };
}

export function workbenchReducer(
  state: WorkbenchState,
  action: WorkbenchAction,
): WorkbenchState {
  switch (action.type) {
    case "SET_PROJECT_ID":
      // 切换项目：回到该项目的默认视图
      if (action.id === state.projectId) return state;
      return {
        ...createWorkbenchState(action.id),
        activeWorkspace: state.activeWorkspace,
      };
    case "SET_WORKSPACE":
      // 仅用户主动切换；各工作区各自的视图保持不变（切换只改 active），来源区/片段上下文不动
      if (action.workspace === state.activeWorkspace) return state;
      return { ...state, activeWorkspace: action.workspace };
    case "SET_INITIAL_WORKSPACE":
      // 加载项目时设定起始标签（空项目落“切分”）；只在项目刚载入时由 page 触发一次
      if (action.workspace === state.activeWorkspace) return state;
      return { ...state, activeWorkspace: action.workspace };
    case "JUMP_FRAGMENT": {
      // 片段跳转：记忆当前片段的工作上下文 → 切到目标片段及其上下文（恢复 task + 选中）
      const prev = state.activeFragmentId;
      const contexts = prev
        ? {
            ...state.contexts,
            [prev]: {
              task: state.activeWorkspace,
              selected: state.byWorkspace[state.activeWorkspace].selectedId,
            },
          }
        : state.contexts;
      return {
        ...state,
        activeFragmentId: action.id,
        contexts,
        activeWorkspace: action.task,
        byWorkspace: {
          ...state.byWorkspace,
          [action.task]: {
            ...state.byWorkspace[action.task],
            selectedId: action.selected,
          },
        },
      };
    }
    case "SELECT":
      return updateActive(state, (v) => ({ ...v, selectedId: action.id }));
    case "SET_VIEWPORT":
      return updateActive(state, (v) => ({ ...v, viewport: action.viewport }));
    case "SET_LAYOUT":
      return updateActive(state, (v) => ({
        ...v,
        layout: {
          ...(v.layout ?? {}),
          [action.id]: { x: action.x, y: action.y },
        },
      }));
    case "SET_SOURCE_VIEW":
      return action.view === state.sourceView
        ? state
        : { ...state, sourceView: action.view };
    case "SET_SOURCE_OPEN":
      return action.open === state.sourceOpen
        ? state
        : { ...state, sourceOpen: action.open };
    case "SET_PENDING_RANGE":
      return { ...state, pendingRange: action.range };
    case "SET_INSPECTOR":
      return action.open === state.inspectorOpen
        ? state
        : { ...state, inspectorOpen: action.open };
    case "ADD_CANDIDATE_FRAGMENT":
      if (state.candidateFragments.some((f) => f.id === action.fragment.id))
        return state;
      return {
        ...state,
        candidateFragments: [...state.candidateFragments, action.fragment],
      };
    case "REMOVE_CANDIDATE_FRAGMENT":
      return {
        ...state,
        candidateFragments: state.candidateFragments.filter(
          (f) => f.id !== action.id,
        ),
      };
    case "RENAME_CANDIDATE_FRAGMENT":
      return {
        ...state,
        candidateFragments: state.candidateFragments.map((f) =>
          f.id === action.id ? { ...f, name: action.name } : f,
        ),
      };
    default:
      return state;
  }
}
