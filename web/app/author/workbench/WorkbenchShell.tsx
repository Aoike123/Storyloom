'use client';
import Link from 'next/link';
import { ArrowLeft, PanelLeft, Info } from 'lucide-react';
import WorkspaceTabs from './WorkspaceTabs';
import ContextPanel from './ContextPanel';
import CanvasViewport from './CanvasViewport';
import ReleaseWorkspace from './ReleaseWorkspace';
import SourceArea from './SourceArea';
import ConversationBar from './ConversationBar';
import { WORKSPACE_LABELS, type WorkbenchState, type WorkbenchAction, type Workspace } from './workspace-state';
import { type DisplayNode, type WorkspaceContent } from './display-model';
import { type SourceFragment, type SourceModel } from './source-model';

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

// 从片段正文里提取第一句引号对白（示例剧本用；真实对白拆分在 B 批）。
function firstDialogue(text: string): string | null {
  const m = /“([^”]{2,40})”|「([^」]{2,40})」|“([^”]{2,40})”/.exec(text);
  const q = m ? (m[1] ?? m[2] ?? m[3]) : null;
  return q ? q : null;
}

// 中央画布节点：分镜/资产/成片用真实数据；切分/剧本每个片段各一个节点。
// 剧本 = 节点内含「动作 / 对白」两个可编辑字段；切分 = 节点内含「裁减正文」字段。
// 片段是真实原文范围，正文取自真实文本；字段留空走空态，仅对白取自原文引号（非伪造）。
// 片段未在服务端绑定（D03 待定）→ 标记 isExample（不伪造已保存记录）。
function buildFragmentNode(task: 'script' | 'cut', frag: SourceFragment, source: SourceModel): DisplayNode {
  const text = source.text.slice(frag.range.start, frag.range.end);
  const body = text.length > 54 ? text.slice(0, 54) + '…' : text;
  const sourceRef = {
    fragmentName: frag.name,
    rangeStart: frag.range.start,
    rangeEnd: frag.range.end,
    sourceRevision: source.revision,
  };
  if (task === 'cut') {
    return { id: frag.id, kind: 'cut', title: frag.name, group: frag.name, body, fields: [{ label: '裁减正文', value: null }], version: null, sourceRef, isExample: true };
  }
  return {
    id: frag.id,
    kind: 'script',
    title: frag.name,
    group: frag.name,
    body,
    fields: [
      { label: '动作', value: null },
      { label: '对白', value: firstDialogue(text) },
    ],
    version: null,
    sourceRef,
    isExample: true,
  };
}

interface Props {
  view: WorkbenchState;
  dispatch: (a: WorkbenchAction) => void;
  title?: string | null;
  author?: string | null;
  stage?: string | null;
  readError?: string | null;
  hasWork?: boolean;
  source?: SourceModel | null;
  content?: Record<Workspace, WorkspaceContent> | null;
}

// 以「项目」为核心：项目顶栏 + 五个工作标签 + 三区主体（左：来源区｜中：任务画布/成片｜右：属性面板）。
// 左侧来源区贯穿项目，跨工作区共享；点击荧光区域/片段 = 片段跳转（恢复目标片段的工作上下文）。
export default function WorkbenchShell({ view, dispatch, title, author, stage, readError, hasWork, source, content }: Props) {
  const active = view.activeWorkspace;
  const activeView = view.byWorkspace[active];
  const activeContent = content?.[active] ?? null;

  const allFragments = source ? [...source.fragments, ...view.candidateFragments].sort((a, b) => a.range.start - b.range.start) : [];

  // 中央节点：分镜/资产/成片用真实数据；切分/剧本每个片段各一个节点（剧本=动作/对白字段，切分=裁减字段）
  let nodes: DisplayNode[] = [];
  const emptyReasons: Record<string, string> = activeContent?.emptyReasons ?? {};
  if (active === 'board' || active === 'assets' || active === 'film') {
    nodes = activeContent?.nodes ?? [];
  } else if (source) {
    nodes = allFragments.map((f) => buildFragmentNode(active, f, source));
  }
  const selectedNode = nodes.find((n) => n.id === activeView.selectedId) ?? null;
  const emptyList = Object.values(emptyReasons).filter(Boolean) as string[];
  const loading = !!hasWork && !content;
  const emptyHint = loading
    ? '正在读取项目…'
    : nodes.length === 0
      ? emptyList.length > 0
        ? emptyList.join('；')
        : `「${WORKSPACE_LABELS[active]}」工作区暂无内容`
      : undefined;

  // 片段跳转：记忆当前片段上下文 → 进入目标片段及其上下文（恢复 task + 选中）
  function jump(f: SourceFragment) {
    const saved = view.contexts[f.id];
    const task: Workspace = saved?.task ?? f.task;
    const selected = saved?.selected ?? (task === 'script' || task === 'cut' ? f.id : null);
    dispatch({ type: 'JUMP_FRAGMENT', id: f.id, task, selected });
  }

  return (
    <div className="workbench" data-source={view.sourceOpen} data-inspector={view.inspectorOpen}>
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
            aria-pressed={view.sourceOpen}
            title={view.sourceOpen ? '收起来源区' : '展开来源区'}
            onClick={() => dispatch({ type: 'SET_SOURCE_OPEN', open: !view.sourceOpen })}
          >
            <PanelLeft size={15} />
            来源
          </button>
          <button
            type="button"
            className="button secondary"
            aria-pressed={view.inspectorOpen}
            title={view.inspectorOpen ? '收起属性面板' : '展开属性面板'}
            onClick={() => dispatch({ type: 'SET_INSPECTOR', open: !view.inspectorOpen })}
          >
            <Info size={15} />
            属性
          </button>
          <Link className="button secondary" href="/">
            <ArrowLeft size={15} />
            返回
          </Link>
        </div>
      </header>

      <WorkspaceTabs active={active} onSelect={(w: Workspace) => dispatch({ type: 'SET_WORKSPACE', workspace: w })} />

      <div className="workbench-body">
        <SourceArea
          source={source ?? { revision: 'import-0', text: '', paragraphs: [], fragments: [], isExample: false }}
          open={view.sourceOpen}
          view={view.sourceView}
          activeFragmentId={view.activeFragmentId}
          active={active}
          candidateFragments={view.candidateFragments}
          pendingRange={view.pendingRange}
          onToggleView={(v) => dispatch({ type: 'SET_SOURCE_VIEW', view: v })}
          onCollapse={() => dispatch({ type: 'SET_SOURCE_OPEN', open: false })}
          onJump={jump}
          onSetPending={(r) => dispatch({ type: 'SET_PENDING_RANGE', range: r })}
          onAddCandidate={(f) => dispatch({ type: 'ADD_CANDIDATE_FRAGMENT', fragment: f })}
        />

        <main className="workbench-main">
          {readError ? <div className="workbench-notice" role="alert">读取项目失败：{readError}</div> : null}
          {!hasWork ? (
            <div className="workbench-notice">
              尚未绑定项目：请通过 <code>/author/workbench?work=&lt;id&gt;</code> 打开一个已有项目。
            </div>
          ) : null}
          {active === 'film' ? (
            <ReleaseWorkspace nodes={nodes} selectedId={activeView.selectedId} onSelect={(id) => dispatch({ type: 'SELECT', id })} />
          ) : (
            <>
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
              <ConversationBar
                stepLabel={`${WORKSPACE_LABELS[active]}步骤`}
                contextLabel={selectedNode ? `针对「${selectedNode.title}」` : null}
              />
            </>
          )}
        </main>

        <ContextPanel
          open={view.inspectorOpen}
          selectedNode={selectedNode}
          onClose={() => dispatch({ type: 'SET_INSPECTOR', open: false })}
        />
      </div>
    </div>
  );
}
