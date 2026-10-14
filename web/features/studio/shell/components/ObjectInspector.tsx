"use client";

// ---------------------------------------------------------------------------
// VIEW-03 · ObjectInspector：统一对象属性面板（完全受控）
//
// 受控契约：
//   · 属性数据只来自 props.view（由 shell/object-view.ts 的 deriveObjectView 派生）；
//     组件不取数、不发网络、不碰浏览器存储，也不用组件状态镜像业务数据。
//   · view === null → 空态（不造占位字段、不显示假属性）。
//   · 渲染对象 ref、归属（真实父级）、出处链、版本、状态与字段；清除选择经
//     onClear 上抛，组件自己不修改选择。
// ---------------------------------------------------------------------------

import { kindLabel } from "../object-view";
import type { ObjectView } from "../object-view";

export interface ObjectInspectorProps {
  /** 受控属性视图模型；null = 未选择对象（空态）。 */
  view: ObjectView | null;
  /** 清除选择（可选）：组件只上抛，不自己改选择。 */
  onClear?: () => void;
}

function formatRef(kind: string, id: string, revision: number | null): string {
  return `${kindLabel(kind)} ${id}${revision === null ? "" : ` · r${revision}`}`;
}

function formatFieldValue(value: string | number | boolean | null): string {
  if (value === null) return "未知";
  return String(value);
}

export default function ObjectInspector({ view, onClear }: ObjectInspectorProps) {
  if (view === null) {
    return (
      <aside className="object-inspector object-inspector--empty" data-selection="none">
        <p className="object-inspector__empty">未选择对象。</p>
        <style jsx>{`
          .object-inspector {
            box-sizing: border-box;
            width: 100%;
            padding: 1rem;
            font-family:
              -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
              "Noto Sans", sans-serif;
            color: #1a1a1a;
            background: #fff;
            border: 1px solid #e2e2e2;
            border-radius: 8px;
          }
          .object-inspector--empty {
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 120px;
            background: #fafafa;
          }
          .object-inspector__empty {
            margin: 0;
            font-size: 0.9375rem;
            color: #888;
          }
        `}</style>
      </aside>
    );
  }

  const ref = view.object_ref;

  return (
    <aside
      className="object-inspector"
      data-object-kind={ref.kind}
      data-object-id={ref.id}
      data-object-revision={ref.revision === null ? "" : ref.revision}
    >
      <header className="object-inspector__header">
        <div className="object-inspector__heading">
          <span className="object-inspector__kind">{view.kind_label}</span>
          <span className="object-inspector__title">
            {view.title === "" ? "（未命名）" : view.title}
          </span>
        </div>
        {onClear === undefined ? null : (
          <button type="button" className="object-inspector__clear" onClick={onClear}>
            清除选择
          </button>
        )}
      </header>

      {view.available ? null : (
        <p className="object-inspector__notice">
          该对象所选 revision 的来源数据不可用（未加载或版本已变化），属性保守留空。
        </p>
      )}

      <dl className="object-inspector__meta">
        <div className="object-inspector__row">
          <dt className="object-inspector__term">对象</dt>
          <dd className="object-inspector__detail">
            {ref.kind} / {ref.id}
          </dd>
        </div>
        <div className="object-inspector__row">
          <dt className="object-inspector__term">版本</dt>
          <dd className="object-inspector__detail">
            {view.version.revision === null ? "无版本" : `revision ${view.version.revision}`}
            {view.version.label === null ? "" : ` · ${view.version.label}`}
          </dd>
        </div>
        <div className="object-inspector__row">
          <dt className="object-inspector__term">状态</dt>
          <dd className="object-inspector__detail">{view.state === null ? "未知" : view.state}</dd>
        </div>
        <div className="object-inspector__row">
          <dt className="object-inspector__term">归属</dt>
          <dd className="object-inspector__detail">
            {view.attribution === null
              ? "无真实父级"
              : formatRef(
                  view.attribution.kind,
                  view.attribution.id,
                  view.attribution.revision,
                )}
          </dd>
        </div>
      </dl>

      <section className="object-inspector__section">
        <h3 className="object-inspector__section-title">出处</h3>
        {view.provenance.length === 0 ? (
          <p className="object-inspector__none">无出处记录。</p>
        ) : (
          <ul className="object-inspector__list">
            {view.provenance.map((entry, index) => (
              <li key={`prov:${entry.kind}:${entry.id}:${index}`} className="object-inspector__item">
                {formatRef(entry.kind, entry.id, entry.revision)}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="object-inspector__section">
        <h3 className="object-inspector__section-title">字段</h3>
        {view.fields.length === 0 ? (
          <p className="object-inspector__none">无字段记录。</p>
        ) : (
          <ul className="object-inspector__list">
            {view.fields.map((field) => (
              <li key={field.key} className="object-inspector__item">
                <span className="object-inspector__field-label">{field.label}</span>
                <span className="object-inspector__field-value">
                  {formatFieldValue(field.value)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <style jsx>{`
        .object-inspector {
          box-sizing: border-box;
          width: 100%;
          padding: 1rem;
          font-family:
            -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
            "Noto Sans", sans-serif;
          color: #1a1a1a;
          background: #fff;
          border: 1px solid #e2e2e2;
          border-radius: 8px;
        }
        .object-inspector__header {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 0.5rem;
          margin-bottom: 0.75rem;
        }
        .object-inspector__heading {
          display: flex;
          flex-direction: column;
          gap: 0.125rem;
          min-width: 0;
        }
        .object-inspector__kind {
          font-size: 0.6875rem;
          color: #777;
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }
        .object-inspector__title {
          font-size: 1rem;
          font-weight: 600;
          line-height: 1.3;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .object-inspector__clear {
          flex: none;
          padding: 0.25rem 0.5rem;
          border: 1px solid #ccc;
          border-radius: 4px;
          background: #fff;
          font-size: 0.8125rem;
          cursor: pointer;
        }
        .object-inspector__notice {
          margin: 0 0 0.75rem;
          padding: 0.375rem 0.5rem;
          font-size: 0.8125rem;
          color: #8a5a00;
          background: #fff8e6;
          border: 1px solid #f0dfb0;
          border-radius: 4px;
        }
        .object-inspector__meta {
          margin: 0;
        }
        .object-inspector__row {
          display: flex;
          gap: 0.5rem;
          padding: 0.25rem 0;
          border-top: 1px solid #f0f0f0;
        }
        .object-inspector__term {
          flex: none;
          width: 3.5rem;
          font-size: 0.8125rem;
          color: #777;
        }
        .object-inspector__detail {
          margin: 0;
          font-size: 0.875rem;
          word-break: break-all;
        }
        .object-inspector__section {
          margin-top: 0.75rem;
        }
        .object-inspector__section-title {
          margin: 0 0 0.25rem;
          font-size: 0.8125rem;
          color: #555;
        }
        .object-inspector__list {
          margin: 0;
          padding-left: 1rem;
        }
        .object-inspector__item {
          display: flex;
          justify-content: space-between;
          gap: 0.5rem;
          font-size: 0.875rem;
          line-height: 1.5;
        }
        .object-inspector__field-label {
          color: #777;
        }
        .object-inspector__field-value {
          word-break: break-all;
        }
        .object-inspector__none {
          margin: 0;
          font-size: 0.875rem;
          color: #999;
        }
      `}</style>
    </aside>
  );
}
