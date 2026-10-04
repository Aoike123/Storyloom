"use client";
import { X } from "lucide-react";
import { NODE_KIND_LABELS, type DisplayNode } from "./display-model";

interface Props {
  open: boolean;
  selectedNode?: DisplayNode | null;
  onClose: () => void;
}

// 右侧属性面板：跟随所选对象，展示其真实字段 + 来源出处 + 当前版本。
// 缺失字段走空态（F0 §9），不伪造；示例对象标注“未写库”。见 design-spec §7/§8。
export default function ContextPanel({ open, selectedNode, onClose }: Props) {
  if (!open) return null; // 关闭时画布重新获得空间
  const n = selectedNode;
  return (
    <aside className="workbench-panel" aria-label="属性">
      <div className="workbench-panel-head">
        <span className="workbench-panel-title">属性</span>
        <button
          type="button"
          className="button icon"
          aria-label="关闭属性面板"
          onClick={onClose}
        >
          <X size={16} />
        </button>
      </div>

      {!n ? (
        <p className="workbench-panel-empty">
          未选择对象。点击画布中的节点，在此查看它的字段、来源与版本。
        </p>
      ) : (
        <div className="workbench-inspector">
          <div className="workbench-inspector-identity">
            <span className="workbench-panel-kind">
              {NODE_KIND_LABELS[n.kind]}
            </span>
            <strong className="workbench-panel-node-title">{n.title}</strong>
            {n.group ? (
              <small className="workbench-panel-group">归属：{n.group}</small>
            ) : null}
            {n.isExample ? (
              <small className="workbench-panel-example">
                示例数据 · 未写库
              </small>
            ) : null}
          </div>

          {n.fields && n.fields.length > 0 ? (
            <section className="workbench-inspector-section">
              <h4>字段</h4>
              <ul className="workbench-inspector-fields">
                {n.fields.map((f) => (
                  <li key={f.label} className="workbench-inspector-field">
                    <span className="workbench-inspector-field-label">
                      {f.label}
                    </span>
                    {f.value ? (
                      <span className="workbench-inspector-field-value">
                        {f.value}
                      </span>
                    ) : (
                      <span className="workbench-inspector-field-empty">
                        待填写
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="workbench-inspector-section">
            <h4>来源</h4>
            {n.sourceRef ? (
              <p className="workbench-inspector-src">
                来自片段「{n.sourceRef.fragmentName}」
                <small>
                  正文 {n.sourceRef.sourceRevision} · 第{" "}
                  {n.sourceRef.rangeStart}–{n.sourceRef.rangeEnd} 字
                </small>
              </p>
            ) : (
              <p className="workbench-inspector-empty">无来源关联</p>
            )}
          </section>

          <section className="workbench-inspector-section">
            <h4>版本</h4>
            {n.version ? (
              <p className="workbench-inspector-ver">{n.version}</p>
            ) : (
              <p className="workbench-inspector-empty">未记录版本</p>
            )}
          </section>
        </div>
      )}
    </aside>
  );
}
