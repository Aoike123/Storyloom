"use client";
import type { NodeProps } from "@xyflow/react";
import { type FieldDef } from "./display-model";

// 预览节点：镜头/素材/成片等媒体单元（缩略图），或剧本/裁减等文本单元（原文参考 + 可编辑字段）。
// 字段 value 为空 → 空态“待填写”（不伪造正文）。真实字段编辑/属性展示在 D 批属性面板。
export default function PreviewNode(props: NodeProps) {
  const d = props.data as {
    title: string;
    kind?: string;
    isExample?: boolean;
    body?: string;
    fields?: FieldDef[];
  };
  const isText = d.kind === "script" || d.kind === "cut";
  const cls =
    "workbench-node workbench-preview" +
    (d.kind ? " workbench-preview-" + d.kind : "") +
    (isText ? " workbench-preview-text" : "") +
    (props.selected ? " is-selected" : "") +
    (d.isExample ? " is-example" : "");
  return (
    <div className={cls}>
      {!isText ? (
        <div className="workbench-preview-thumb" aria-hidden="true" />
      ) : null}
      <div className="workbench-preview-title">{d.title}</div>
      {d.body ? (
        <div className="workbench-node-body">
          <span className="workbench-node-body-label">原文</span>
          {d.body}
        </div>
      ) : null}
      {d.fields && d.fields.length > 0 ? (
        <div className="workbench-node-fields">
          {d.fields.map((f) => (
            <div key={f.label} className="workbench-node-field">
              <span className="workbench-node-field-label">{f.label}</span>
              {f.value ? (
                <span className="workbench-node-field-value">{f.value}</span>
              ) : (
                <span className="workbench-node-field-empty">待填写</span>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
