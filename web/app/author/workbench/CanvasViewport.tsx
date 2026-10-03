'use client';
import { ReactFlow, ReactFlowProvider, Background, BackgroundVariant, useReactFlow, useNodesState, type Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Maximize } from 'lucide-react';
import SceneFrameNode from './SceneFrame';
import PreviewNode from './PreviewNode';
import { type DisplayNode } from './display-model';
import { type Viewport } from './workspace-state';

// 只暴露画布能力：平移(拖空白) / 缩放(滚轮) / 适配(自有按钮) / 节点自由摆放 / 单选。
// 明确禁用：连线(nodesConnectable=false)、多选(selectionKeyCode/multiSelectionKeyCode=null)、删除(deleteKeyCode=null)、
// 以及 React Flow 自带 Controls 控件面板（改用工作台自有的 workbench-canvas-fit 适配按钮，见 F0 §6）。
const nodeTypes = {
  scene: SceneFrameNode,
  preview: PreviewNode,
} as const;

function toRfNode(
  n: DisplayNode,
  fallback: { x: number; y: number },
  layout?: Record<string, { x: number; y: number }>,
): Node {
  return {
    id: n.id,
    type: n.kind === 'scene' ? 'scene' : 'preview',
    position: layout?.[n.id] ?? fallback,
    data: { title: n.title, kind: n.kind, isExample: n.isExample ?? false },
  };
}

// 工作台自有的「适配视野」按钮（替代 React Flow 自带 Controls 控件面板）
function FitViewButton() {
  const { fitView } = useReactFlow();
  return (
    <button
      type="button"
      className="button icon workbench-canvas-fit"
      title="适配视野"
      aria-label="适配视野"
      onClick={() => fitView({ duration: 200 })}
    >
      <Maximize size={15} />
    </button>
  );
}

// 按场整齐排列：同 group 的节点排成一行（场次节点在前、其余横排），行与行纵向堆叠；
// 用户自由摆卡（layout）优先于这里的默认坐标。
function defaultLayout(nodes: DisplayNode[]): Record<string, { x: number; y: number }> {
  const groups = new Map<string, DisplayNode[]>();
  for (const n of nodes) {
    const key = n.group ?? n.kind;
    const arr = groups.get(key);
    if (arr) arr.push(n);
    else groups.set(key, [n]);
  }
  const out: Record<string, { x: number; y: number }> = {};
  let row = 0;
  for (const [, gns] of groups) {
    const y = 80 + row * 220;
    let x = 80;
    for (const n of gns) {
      out[n.id] = { x, y };
      x += (n.kind === 'scene' ? 320 : 150) + 24;
    }
    row += 1;
  }
  return out;
}

interface Props {
  canvasKey: string;
  nodes: DisplayNode[];
  layout?: Record<string, { x: number; y: number }>;
  viewport?: Viewport;
  selectedId?: string | null;
  onViewportChange?: (v: Viewport) => void;
  onSelect?: (id: string | null) => void;
  onNodeDragStop?: (id: string, x: number, y: number) => void;
  emptyHint?: string;
}

function CanvasInner({ nodes, layout, viewport, onViewportChange, onSelect, onNodeDragStop, emptyHint }: Props) {
  const defaults = defaultLayout(nodes);
  const initial = nodes.map((n) => toRfNode(n, defaults[n.id] ?? { x: 80, y: 80 }, layout));
  const [rfNodes, , onNodesChange] = useNodesState(initial);
  // 首次进入该工作区（视口仍为默认 {0,0,1}）时自动适配视野；之后恢复已保存的视野
  const isPristine = !!viewport && viewport.x === 0 && viewport.y === 0 && viewport.scale === 1;

  return (
    <div className="workbench-canvas">
      <ReactFlow
        nodes={rfNodes}
        edges={[]}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onNodeClick={(_e, node) => onSelect?.(node.id)}
        onPaneClick={() => onSelect?.(null)}
        onNodeDragStop={(_e, node) => onNodeDragStop?.(node.id, node.position.x, node.position.y)}
        onMove={(_e, vp) => onViewportChange?.({ x: vp.x, y: vp.y, scale: vp.zoom })}
        onInit={(instance) => {
          if (isPristine && nodes.length > 0) instance.fitView({ duration: 0 });
        }}
        defaultViewport={viewport ? { x: viewport.x, y: viewport.y, zoom: viewport.scale } : undefined}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        panOnDrag
        zoomOnScroll
        deleteKeyCode={null}
        selectionKeyCode={null}
        multiSelectionKeyCode={null}
        minZoom={0.2}
        maxZoom={2}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--line)" />
      </ReactFlow>
      <FitViewButton />
      {nodes.length === 0 ? <div className="workbench-canvas-empty">{emptyHint ?? '本工作区暂无内容'}</div> : null}
      {nodes.length > 0 ? <div className="workbench-canvas-tag">{nodes.length} 个节点</div> : null}
    </div>
  );
}

// 外层 Provider + key：切换工作区时整块重建，载入该工作区各自的节点 / 视口 / 布局（按 项目 × 工作区 隔离）
export default function CanvasViewport(props: Props) {
  return (
    <ReactFlowProvider>
      <CanvasInner key={props.canvasKey} {...props} />
    </ReactFlowProvider>
  );
}
