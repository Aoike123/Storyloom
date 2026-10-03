'use client';
import { X } from 'lucide-react';
import { NODE_KIND_LABELS, type DisplayNode } from './display-model';

interface Props {
  open: boolean;
  selectedNode?: DisplayNode | null;
  onClose: () => void;
}

const SLOTS = ['属性', '引用', '任务', '定位'] as const;

// 右侧按需上下文面板：可开合；本期只放只读摘要 + 占位插槽（属性/引用/任务/定位），
// 无编辑/生成表单、无版本入口。见 f0-framework-contract.md §3/§8。
export default function ContextPanel({ open, selectedNode, onClose }: Props) {
  if (!open) return null; // 关闭时画布重新获得空间
  return (
    <aside className="workbench-panel" aria-label="上下文">
      <div className="workbench-panel-head">
        <span className="workbench-panel-title">上下文</span>
        <button type="button" className="button icon" aria-label="关闭上下文面板" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      {selectedNode ? (
        <div className="workbench-panel-summary">
          <span className="workbench-panel-kind">{NODE_KIND_LABELS[selectedNode.kind]}</span>
          <strong className="workbench-panel-node-title">{selectedNode.title}</strong>
          {selectedNode.group ? <small className="workbench-panel-group">归属：{selectedNode.group}</small> : null}
          {selectedNode.isExample ? <small className="workbench-panel-example">示例数据</small> : null}
        </div>
      ) : (
        <p className="workbench-panel-summary">未选择对象：点击画布中的节点查看摘要。</p>
      )}
      <ul className="workbench-panel-slots">
        {SLOTS.map((s) => (
          <li key={s} className="workbench-slot-placeholder">
            <span>{s}</span>
            <small>占位 · 待讨论</small>
          </li>
        ))}
      </ul>
    </aside>
  );
}
