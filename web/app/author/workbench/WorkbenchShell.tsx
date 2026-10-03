'use client';
import Link from 'next/link';
import { ArrowLeft } from 'lucide-react';
import WorkspaceTabs from './WorkspaceTabs';
import ContextPanel from './ContextPanel';
import CanvasViewport from './CanvasViewport';
import ReleaseWorkspace from './ReleaseWorkspace';
import { WORKSPACE_LABELS, type WorkbenchState, type WorkbenchAction, type Workspace } from './workspace-state';
import { type DisplayNode, type WorkspaceContent } from './display-model';

// 现有 9 个生产阶段的只读中文标签（顶栏进度用，非导航）
const STAGE_LABELS: Record<string, string> = {
  style: '选择风格',
  segments_review: '确认情节',
  preparing: '准备形象',
  assets_review: '确认图片',
  storyboarding: '分镜生成',
  rendering: '漫剧生成',
  episode_review: '本集发布',
  film_review: '审片验收',
  published: '发布作品',
};

interface Props {
  view: WorkbenchState;
  dispatch: (a: WorkbenchAction) => void;
  title?: string | null;
  author?: string | null;
  stage?: string | null;
  readError?: string | null;
  hasWork?: boolean;
  content?: Record<Workspace, WorkspaceContent> | null;
}

// F1/F3/F4 · 工作台外壳：以「项目」为核心——顶栏呈现项目身份与进度；
// 故事/制作/素材/成片是该项目的四个工作区视图：前三个为无限画布，成片为独立容器占位。
export default function WorkbenchShell({ view, dispatch, title, author, stage, readError, hasWork, content }: Props) {
  const active = view.activeWorkspace;
  const activeView = view.byWorkspace[active];
  const panelOpen = activeView.panelOpen;

  const activeContent = content?.[active] ?? null;
  const nodes: DisplayNode[] = activeContent?.nodes ?? [];
  const selectedNode = nodes.find((n) => n.id === activeView.selectedId) ?? null;
  const emptyReasons = activeContent?.emptyReasons ?? {};
  const emptyList = (Object.values(emptyReasons) ?? []).filter((s) => !!s) as string[];
  const loading = !!hasWork && !content;
  const emptyHint = loading
    ? '正在读取项目…'
    : nodes.length === 0
      ? emptyList.length > 0
        ? emptyList.join('；')
        : `「${WORKSPACE_LABELS[active]}」工作区暂无内容`
      : undefined;

  return (
    <div className="workbench">
      <header className="workbench-topbar">
        <Link className="workbench-brand" href="/">
          叙间<span>STORYLOOM · WORKBENCH</span>
        </Link>
        <div className="workbench-topbar-project">
          <span className="workbench-project-name">{title || '未绑定项目'}</span>
          {stage ? <span className="workbench-project-stage">{STAGE_LABELS[stage] ?? stage}</span> : null}
          {author ? <small className="workbench-project-author">{author}</small> : null}
        </div>
        <div className="workbench-topbar-actions">
          <button
            type="button"
            className="button secondary"
            aria-pressed={panelOpen}
            onClick={() => dispatch({ type: 'SET_PANEL', open: !panelOpen })}
          >
            上下文
          </button>
          <Link className="button secondary" href="/">
            <ArrowLeft size={15} />
            返回故事市场
          </Link>
        </div>
      </header>

      <WorkspaceTabs active={active} onSelect={(w: Workspace) => dispatch({ type: 'SET_WORKSPACE', workspace: w })} />

      <div className="workbench-body">
        <main className="workbench-main">
          {readError ? <div className="workbench-notice" role="alert">读取项目失败：{readError}</div> : null}
          {!hasWork ? (
            <div className="workbench-notice">
              尚未绑定项目：请通过 <code>/author/workbench?work=&lt;id&gt;</code> 打开一个已有项目。
            </div>
          ) : null}
          {active === 'release' ? (
            <ReleaseWorkspace nodes={nodes} selectedId={activeView.selectedId} onSelect={(id) => dispatch({ type: 'SELECT', id })} />
          ) : (
            <CanvasViewport
              canvasKey={active}
              nodes={nodes}
              layout={activeView.layout}
              viewport={activeView.viewport}
              selectedId={activeView.selectedId}
              onViewportChange={(v) => dispatch({ type: 'SET_VIEWPORT', viewport: v })}
              onSelect={(id) => dispatch({ type: 'SELECT', id })}
              onNodeDragStop={(id, x, y) => dispatch({ type: 'SET_LAYOUT', id, x, y })}
              emptyHint={emptyHint}
            />
          )}
        </main>
        <ContextPanel open={panelOpen} selectedNode={selectedNode} onClose={() => dispatch({ type: 'SET_PANEL', open: false })} />
      </div>
    </div>
  );
}
