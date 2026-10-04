"use client";
import { useEffect, useRef, useState, type RefObject } from "react";

interface Props {
  bodyRef: RefObject<HTMLDivElement | null>; // .workbench-body 元素（来源区 + 画布的共同父级）
  regionActive: boolean; // 来源区处于原文视图且存在活跃片段（才有可连的区域元素）
  nodeId: string | null; // 要连到的画布节点 id（React Flow 节点 dom id = react-flow__node-{id}）
}

interface Line {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

// 来源连线覆盖层：当前片段在来源区的荧光区域边缘 → 画布中该片段节点（每片段一条线）。
// 用 getBoundingClientRect 取两端相对 body 的坐标；rAF 连续测量以跟随 来源区滚动 / 画布平移缩放 / 窗口 resize。
// 注：这是跨区域(来源区↔画布)的视觉血缘提示；坐标随滚动实时重算，坐标未变化时不触发重渲染。
export default function SourceLines({ bodyRef, regionActive, nodeId }: Props) {
  const [line, setLine] = useState<Line | null>(null);
  const last = useRef<Line | null>(null);
  const active = regionActive && !!nodeId;

  useEffect(() => {
    if (!active) {
      last.current = null;
      setLine(null);
      return;
    }
    let raf = 0;
    const measure = () => {
      const body = bodyRef.current;
      if (body) {
        const region = body.querySelector<HTMLElement>(
          ".workbench-region.is-active",
        );
        const node = body.querySelector<HTMLElement>(
          "#react-flow__node-" + nodeId,
        );
        if (region && node) {
          const b = body.getBoundingClientRect();
          const r = region.getBoundingClientRect();
          const n = node.getBoundingClientRect();
          const next: Line = {
            x1: r.right - b.left,
            y1: r.top + r.height / 2 - b.top,
            x2: n.left - b.left,
            y2: n.top + n.height / 2 - b.top,
          };
          const p = last.current;
          if (
            !p ||
            Math.abs(p.x1 - next.x1) +
              Math.abs(p.y1 - next.y1) +
              Math.abs(p.x2 - next.x2) +
              Math.abs(p.y2 - next.y2) >
              1
          ) {
            last.current = next;
            setLine(next);
          }
        } else if (last.current) {
          last.current = null;
          setLine(null);
        }
      }
      raf = requestAnimationFrame(measure);
    };
    raf = requestAnimationFrame(measure);
    return () => cancelAnimationFrame(raf);
  }, [active, nodeId, bodyRef]);

  if (!line)
    return <svg className="workbench-source-lines" aria-hidden="true" />;
  return (
    <svg className="workbench-source-lines" aria-hidden="true">
      <line x1={line.x1} y1={line.y1} x2={line.x2} y2={line.y2} />
      <circle cx={line.x1} cy={line.y1} r={3} />
      <circle cx={line.x2} cy={line.y2} r={3} />
    </svg>
  );
}
