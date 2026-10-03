'use client';
import { WORKSPACES, WORKSPACE_LABELS, type Workspace } from './workspace-state';

interface Props {
  active: Workspace;
  onSelect: (w: Workspace) => void;
}

// 四个固定工作区；仅用户点击才切换（不承载后端 stage 语义，不自动跳转）。
export default function WorkspaceTabs({ active, onSelect }: Props) {
  return (
    <nav className="workbench-tabs" aria-label="工作区">
      {WORKSPACES.map((w) => (
        <button
          key={w}
          type="button"
          className={'button tab-control' + (w === active ? ' selected' : '')}
          aria-pressed={w === active}
          onClick={() => onSelect(w)}
        >
          {WORKSPACE_LABELS[w]}
        </button>
      ))}
      <span className="workbench-tabs-note">工作标签 · 暂定</span>
    </nav>
  );
}
