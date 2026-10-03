'use client';
import { X } from 'lucide-react';

interface Props {
  open: boolean;
  selectedId: string | null;
  onClose: () => void;
}

const SLOTS = ['属性', '引用', '任务', '定位'] as const;

// 右侧按需上下文面板：可开合；本期只放只读摘要与占位插槽（属性/引用/任务/定位），
// 无编辑/生成表单、无版本入口。见 f0-framework-contract.md §3/§8。
export default function ContextPanel({ open, selectedId, onClose }: Props) {
  if (!open) return null; // 关闭时画布重新获得空间
  return (
    <aside className="workbench-panel" aria-label="上下文">
      <div className="workbench-panel-head">
        <span className="workbench-panel-title">上下文</span>
        <button type="button" className="button icon" aria-label="关闭上下文面板" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <p className="workbench-panel-summary">{selectedId ? `已选中：${selectedId}` : '未选择对象'}</p>
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
