'use client';
import type { NodeProps } from '@xyflow/react';

// 预览节点：镜头 / 素材 / 成片等媒体单元（自定义 React Flow 节点）
export default function PreviewNode(props: NodeProps) {
  const d = props.data as { title: string; kind?: string; isExample?: boolean };
  const cls =
    'workbench-node workbench-preview' +
    (d.kind ? ' workbench-preview-' + d.kind : '') +
    (props.selected ? ' is-selected' : '') +
    (d.isExample ? ' is-example' : '');
  return (
    <div className={cls}>
      <div className="workbench-preview-thumb" aria-hidden="true" />
      <div className="workbench-preview-title">{d.title}</div>
    </div>
  );
}
