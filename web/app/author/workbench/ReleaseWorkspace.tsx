"use client";
import { type DisplayNode } from "./display-model";

interface Props {
  nodes: DisplayNode[];
  selectedId?: string | null;
  onSelect: (id: string) => void;
}

// 成片工作区：独立容器占位（F0 §2）——不接剪辑时间轴内核；只读展示已生成的成片片段，
// 与 story/production/assets 的无限画布明确区分。
export default function ReleaseWorkspace({
  nodes,
  selectedId,
  onSelect,
}: Props) {
  return (
    <div className="workbench-release">
      <div className="workbench-release-note">
        <strong>成片</strong>
        <small>
          剪辑时间轴内核不在本期框架范围（留待后续 D
          任务）；以下为已生成的成片片段，只读展示。
        </small>
      </div>
      {nodes.length === 0 ? (
        <div className="workbench-release-empty">尚无成片片段</div>
      ) : (
        <ul className="workbench-release-list">
          {nodes.map((n) => (
            <li
              key={n.id}
              className={
                "workbench-release-item" +
                (selectedId === n.id ? " is-selected" : "")
              }
              onClick={() => onSelect(n.id)}
            >
              <div className="workbench-release-thumb" aria-hidden="true" />
              <div className="workbench-release-meta">
                <span className="workbench-release-title">{n.title}</span>
                {n.group ? <small>{n.group}</small> : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
