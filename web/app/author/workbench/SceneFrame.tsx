"use client";
import type { NodeProps } from "@xyflow/react";

// 场次 / 片段容器节点（自定义 React Flow 节点）
export default function SceneFrameNode(props: NodeProps) {
  const d = props.data as {
    title: string;
    isExample?: boolean;
    count?: number;
  };
  const cls =
    "workbench-node workbench-sceneframe" +
    (props.selected ? " is-selected" : "") +
    (d.isExample ? " is-example" : "");
  return (
    <div className={cls}>
      <div className="workbench-sceneframe-head">
        <span>{d.title}</span>
        {d.isExample ? <em>示例</em> : null}
      </div>
      <div className="workbench-sceneframe-body">
        {typeof d.count === "number" ? `${d.count} 个分镜` : "场次 / 片段容器"}
      </div>
    </div>
  );
}
