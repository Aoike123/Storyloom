// F1 · 工作台框架：视图状态契约（见 f0-framework-contract.md §5）
// 只管理前端视图状态；业务状态（归属/顺序/素材绑定/确认）由服务端权威，绝不在这里。

export type Workspace = 'story' | 'production' | 'assets' | 'release';

export const WORKSPACES: readonly Workspace[] = ['story', 'production', 'assets', 'release'];

export const WORKSPACE_LABELS: Record<Workspace, string> = {
  story: '故事',
  production: '制作',
  assets: '素材',
  release: '成片',
};

export interface Viewport {
  x: number;
  y: number;
  scale: number;
}

export interface WorkspaceView {
  viewport: Viewport;
  selectedId: string | null;
  panelOpen: boolean;
  layout?: Record<string, { x: number; y: number }>;
}

export interface WorkbenchState {
  projectId: string;
  activeWorkspace: Workspace;
  byWorkspace: Record<Workspace, WorkspaceView>;
}

export type WorkbenchAction =
  | { type: 'SET_PROJECT_ID'; id: string }
  | { type: 'SET_WORKSPACE'; workspace: Workspace }
  | { type: 'SELECT'; id: string | null }
  | { type: 'SET_PANEL'; open: boolean }
  | { type: 'SET_VIEWPORT'; viewport: Viewport }
  | { type: 'SET_LAYOUT'; id: string; x: number; y: number };

const DEFAULT_VIEWPORT: Viewport = { x: 0, y: 0, scale: 1 };

function emptyWorkspaceView(): WorkspaceView {
  return { viewport: { ...DEFAULT_VIEWPORT }, selectedId: null, panelOpen: false };
}

export function createWorkbenchState(projectId: string): WorkbenchState {
  return {
    projectId,
    activeWorkspace: 'production',
    byWorkspace: {
      story: emptyWorkspaceView(),
      production: emptyWorkspaceView(),
      assets: emptyWorkspaceView(),
      release: emptyWorkspaceView(),
    },
  };
}

// 只改"当前查看的工作区"的视图；其余工作区互不影响（按 项目 × 工作区 隔离）。
function updateActive(state: WorkbenchState, fn: (v: WorkspaceView) => WorkspaceView): WorkbenchState {
  const key = state.activeWorkspace;
  const next = fn(state.byWorkspace[key]);
  if (next === state.byWorkspace[key]) return state;
  return { ...state, byWorkspace: { ...state.byWorkspace, [key]: next } };
}

export function workbenchReducer(state: WorkbenchState, action: WorkbenchAction): WorkbenchState {
  switch (action.type) {
    case 'SET_PROJECT_ID':
      // 切换项目：回到该项目的默认视图
      if (action.id === state.projectId) return state;
      return { ...createWorkbenchState(action.id), activeWorkspace: state.activeWorkspace };
    case 'SET_WORKSPACE':
      // 仅用户主动切换；各工作区各自的视图保持不变（切换只改 active）
      if (action.workspace === state.activeWorkspace) return state;
      return { ...state, activeWorkspace: action.workspace };
    case 'SELECT':
      return updateActive(state, (v) => ({ ...v, selectedId: action.id }));
    case 'SET_PANEL':
      return updateActive(state, (v) => ({ ...v, panelOpen: action.open }));
    case 'SET_VIEWPORT':
      return updateActive(state, (v) => ({ ...v, viewport: action.viewport }));
    case 'SET_LAYOUT':
      return updateActive(state, (v) => ({
        ...v,
        layout: { ...(v.layout ?? {}), [action.id]: { x: action.x, y: action.y } },
      }));
    default:
      return state;
  }
}
