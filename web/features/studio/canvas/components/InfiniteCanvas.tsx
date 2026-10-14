"use client";

// ---------------------------------------------------------------------------
// VIEW-02 · InfiniteCanvas：受控无限画布组件（无内部布局/视口状态）
//
// 受控契约：
//   · nodes / viewport 全部经 props 传入，任何变更（平移、缩放、适配、拖动节点）
//     都经 onViewportChange / onMoveNode 回调上抛；组件自身不持久化、不发网络
//     请求、不访问浏览器存储，也不把 props 镜像进组件状态（只保留拖拽用的
//     ref 与滚轮监听）——因此「拖后更新不复位」：渲染坐标永远等于传入值。
//   · 渲染 nodes.map，节点数量与 DOM 一一对应（不过滤、不丢弃、不排序）。
//   · 指针/滚轮监听在卸载时清理；滚轮用原生非 passive 监听以阻止页面滚动。
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useRef } from "react";

import {
  NODE_HEIGHT,
  NODE_WIDTH,
  fitToNodes,
  nodeKey,
  panBy,
  zoomAt,
} from "../canvas-nodes";
import type { CanvasNode, ObjectRef, Viewport } from "../canvas-nodes";

export interface InfiniteCanvasProps {
  /** 受控节点列表：渲染哪些节点、落在哪里，完全由调用方决定。 */
  nodes: readonly CanvasNode[];
  /** 受控视口：屏幕坐标 = 内容坐标 × zoom + (x, y)。 */
  viewport: Viewport;
  /** 视口变更上抛（平移 / 缩放 / 适配）。 */
  onViewportChange: (viewport: Viewport) => void;
  /** 拖动节点结束前的每次移动都上抛新坐标（内容坐标）。 */
  onMoveNode: (objectRef: ObjectRef, x: number, y: number) => void;
  className?: string;
}

/** 每次滚轮/按钮的缩放倍率。 */
const ZOOM_STEP = 1.2;

interface DragStart {
  pointerId: number;
  /** 指针起点（屏幕坐标）。 */
  startX: number;
  startY: number;
  /** 拖动对象的起点：平移用视口原点，节点用节点坐标（内容坐标）。 */
  originX: number;
  originY: number;
  /** 起点时的缩放：屏幕位移换算内容位移用。 */
  originZoom: number;
  /** 拖动节点时携带其对象身份；平移时为 null。 */
  objectRef: ObjectRef | null;
}

export default function InfiniteCanvas({
  nodes,
  viewport,
  onViewportChange,
  onMoveNode,
  className,
}: InfiniteCanvasProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragStart | null>(null);
  // 原生滚轮监听只挂一次，最新 viewport 经 ref 读取，避免闭包读到旧值。
  const viewportRef = useRef<Viewport>(viewport);

  useEffect(() => {
    viewportRef.current = viewport;
  }, [viewport]);

  useEffect(() => {
    const host = hostRef.current;
    if (host === null) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = host.getBoundingClientRect();
      const anchor = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      const factor = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      onViewportChange(zoomAt(viewportRef.current, factor, anchor));
    };
    host.addEventListener("wheel", onWheel, { passive: false });
    return () => host.removeEventListener("wheel", onWheel);
  }, [onViewportChange]);

  const viewportSize = useCallback((): { width: number; height: number } => {
    const host = hostRef.current;
    if (host === null) return { width: 0, height: 0 };
    const rect = host.getBoundingClientRect();
    return { width: rect.width, height: rect.height };
  }, []);

  // 空白处按下 → 平移画布（起点视口 + 屏幕位移，经 panBy 上抛）。
  const handleBackgroundPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      const host = event.currentTarget;
      host.setPointerCapture(event.pointerId);
      dragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: viewport.x,
        originY: viewport.y,
        originZoom: viewport.zoom,
        objectRef: null,
      };
    },
    [viewport.x, viewport.y, viewport.zoom],
  );

  // 节点上按下 → 拖动节点（起点节点坐标 + 屏幕位移 / 起点缩放，经 onMoveNode 上抛）。
  const handleNodePointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>, node: CanvasNode) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      const host = hostRef.current;
      if (host !== null) host.setPointerCapture(event.pointerId);
      dragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: node.x,
        originY: node.y,
        originZoom: viewport.zoom,
        objectRef: node.object_ref,
      };
    },
    [viewport.zoom],
  );

  const handlePointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (drag === null || drag.pointerId !== event.pointerId) return;
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      if (drag.objectRef === null) {
        onViewportChange(
          panBy({ x: drag.originX, y: drag.originY, zoom: drag.originZoom }, dx, dy),
        );
        return;
      }
      const scale = drag.originZoom > 0 ? drag.originZoom : 1;
      onMoveNode(drag.objectRef, drag.originX + dx / scale, drag.originY + dy / scale);
    },
    [onMoveNode, onViewportChange],
  );

  const endDrag = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (drag === null || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    const host = hostRef.current;
    if (host !== null && host.hasPointerCapture(event.pointerId)) {
      host.releasePointerCapture(event.pointerId);
    }
  }, []);

  const zoomByButton = useCallback(
    (factor: number) => {
      const size = viewportSize();
      onViewportChange(zoomAt(viewport, factor, { x: size.width / 2, y: size.height / 2 }));
    },
    [onViewportChange, viewport, viewportSize],
  );

  const fitToContent = useCallback(() => {
    onViewportChange(fitToNodes(viewport, nodes, viewportSize()));
  }, [nodes, onViewportChange, viewport, viewportSize]);

  return (
    <div
      ref={hostRef}
      className={className === undefined ? "infinite-canvas" : `infinite-canvas ${className}`}
      data-node-count={nodes.length}
      onPointerDown={handleBackgroundPointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    >
      <div className="infinite-canvas__toolbar" onPointerDown={(event) => event.stopPropagation()}>
        <button
          type="button"
          className="infinite-canvas__btn"
          onClick={() => zoomByButton(ZOOM_STEP)}
        >
          放大
        </button>
        <button
          type="button"
          className="infinite-canvas__btn"
          onClick={() => zoomByButton(1 / ZOOM_STEP)}
        >
          缩小
        </button>
        <button type="button" className="infinite-canvas__btn" onClick={fitToContent}>
          适配
        </button>
        <span className="infinite-canvas__zoom">{Math.round(viewport.zoom * 100)}%</span>
      </div>
      <div
        className="infinite-canvas__content"
        style={{
          transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`,
        }}
      >
        {nodes.map((node) => (
          <div
            key={nodeKey(node.object_ref)}
            className="infinite-canvas__node"
            data-object-kind={node.object_ref.kind}
            data-object-id={node.object_ref.id}
            data-object-revision={node.object_ref.revision}
            style={{
              transform: `translate(${node.x}px, ${node.y}px)`,
              width: NODE_WIDTH,
              height: NODE_HEIGHT,
            }}
            onPointerDown={(event) => handleNodePointerDown(event, node)}
          >
            <span className="infinite-canvas__node-kind">{node.kind}</span>
            <span className="infinite-canvas__node-label">{node.label}</span>
          </div>
        ))}
      </div>
      <style jsx>{`
        .infinite-canvas {
          position: relative;
          width: 100%;
          height: 100%;
          min-height: 320px;
          overflow: hidden;
          background: #fafafa;
          background-image:
            linear-gradient(#e8e8e8 1px, transparent 1px),
            linear-gradient(90deg, #e8e8e8 1px, transparent 1px);
          background-size: 24px 24px;
          cursor: grab;
          touch-action: none;
          font-family:
            -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
            "Noto Sans", sans-serif;
          color: #1a1a1a;
        }
        .infinite-canvas__toolbar {
          position: absolute;
          top: 0.5rem;
          left: 0.5rem;
          z-index: 2;
          display: flex;
          align-items: center;
          gap: 0.25rem;
          padding: 0.25rem;
          border: 1px solid #ddd;
          border-radius: 6px;
          background: rgba(255, 255, 255, 0.92);
        }
        .infinite-canvas__btn {
          padding: 0.25rem 0.5rem;
          border: 1px solid #ccc;
          border-radius: 4px;
          background: #fff;
          font-size: 0.8125rem;
          cursor: pointer;
        }
        .infinite-canvas__zoom {
          padding: 0 0.375rem;
          font-size: 0.8125rem;
          color: #555;
        }
        .infinite-canvas__content {
          position: absolute;
          top: 0;
          left: 0;
          /* 屏幕 = 内容 × zoom + 偏移：缩放原点必须在内容左上角。 */
          transform-origin: 0 0;
        }
        .infinite-canvas__node {
          position: absolute;
          top: 0;
          left: 0;
          box-sizing: border-box;
          display: flex;
          flex-direction: column;
          justify-content: center;
          gap: 0.25rem;
          padding: 0.5rem 0.75rem;
          border: 1px solid #c9c9c9;
          border-radius: 8px;
          background: #fff;
          box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
          cursor: move;
          user-select: none;
        }
        .infinite-canvas__node-kind {
          font-size: 0.6875rem;
          color: #777;
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .infinite-canvas__node-label {
          font-size: 0.9375rem;
          line-height: 1.3;
          overflow: hidden;
          white-space: nowrap;
          text-overflow: ellipsis;
        }
      `}</style>
    </div>
  );
}
