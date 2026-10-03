'use client';
import { ReactFlow, ReactFlowProvider, Background, BackgroundVariant, Controls, useNodesState, type Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import SceneFrameNode from './SceneFrame';
import PreviewNode from './PreviewNode';
import { type DisplayNode } from './display-model';
import { type Viewport } from './workspace-state';

// 只暴露画布能力：平移 / 缩放 / fit / 节点自由摆放 / 单选。
// 明确禁用：连线(nodesConnectable=false)、多选(selectionKeyCode/multiSelectionKeyCode=null)、删除(deleteKeyCode=null)——留给业务层 D02/D05。
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
  const initial = nodes.map((n, i) =>
    toRfNode(n, { x: 80 + (i % 2) * 240, y: 80 + Math.floor(i / 2) * 180 }, layout),
  );
  const [rfNodes, , onNodesChange] = useNodesState(initial);

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
        defaultViewport={viewport ? { x: viewport.x, y: viewport.y, zoom: viewport.scale } : undefined}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        panOnDrag
        deleteKeyCode={null}
        selectionKeyCode={null}
        multiSelectionKeyCode={null}
        minZoom={0.2}
        maxZoom={2}
        fitView
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--line)" />
        <Controls />
      </ReactFlow>
      {nodes.length === 0 ? <div className="workbench-canvas-empty">{emptyHint ?? '本工作区暂无内容'}</div> : null}
      <div className="workbench-canvas-tag">示例画布 · F3 接入真实项目数据</div>
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
