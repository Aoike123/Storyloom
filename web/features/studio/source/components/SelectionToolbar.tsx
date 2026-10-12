"use client";

// SOURCE-13 · 原生选区建立候选工具条（契约 v2.9，附录 02 §2 F1）。
//
// 冻结决策（本文件头注记）：
// - pending 态：用户在正文里做原生 DOM 选区后出现，等待输入片段名并保存；
// - 保存：name.trim() === "" → 本地提示（不发请求）；否则
//   createFragmentCandidate → ok：onCreated(object_ref.id)；!ok：setError(res)，
//   保留 name 与 pending（不退出、不清选区）；finally submitting=false；
// - Esc/取消：仅清 pending（onCancel），零 API 调用，禁止任何删除/撤销调用；
//   监听随卸载清理；
// - 错误区：仅 error 非空时显示——range_overlap 列出 conflicts（name + 范围），
//   不自动裁切；revision_conflict 提示刷新后重试；其他显示 error.message；
// - 组件不直接 fetch（走 client）；不 import 旧组件；无硬编码占位文本。

import { useEffect, useState } from "react";

import defaultClient from "@/shared/api/client";
import { createFragmentCandidate } from "../api";
import type { FragmentCreateDto, StudioClient } from "../api";
import type { StudioApiResponse } from "@/shared/api/types";

export interface SelectionToolbarProps {
  projectId: string;
  sourceRevisionId: string;
  range: { start: number; end: number };
  selectedText: string;
  commandId: string;
  onCancel: () => void;
  onCreated: (fragmentId: string) => void;
  client?: StudioClient;
}

type SaveError = StudioApiResponse<FragmentCreateDto> & { ok: false };

export default function SelectionToolbar({
  projectId,
  sourceRevisionId,
  range,
  selectedText,
  commandId,
  onCancel,
  onCreated,
  client,
}: SelectionToolbarProps) {
  const clientObj = client ?? defaultClient;
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<SaveError | null>(null);

  // 挂载期间 Esc → onCancel（仅清 pending；禁止任何删除/撤销 API 调用）；
  // 监听随卸载清理。
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onCancel();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [onCancel]);

  async function handleSave() {
    const trimmed = name.trim();
    if (trimmed === "") {
      // 本地提示：空名不发请求
      setError({
        ok: false,
        code: "validation_failed",
        message: "片段名不能为空。",
        details: {
          violations: [
            {
              field: "name",
              rule: "required",
              message: "片段名不能为空。",
            },
          ],
        },
      } as SaveError);
      return;
    }
    setSubmitting(true);
    try {
      const res = await createFragmentCandidate(clientObj, projectId, {
        sourceRevisionId,
        start: range.start,
        end: range.end,
        name: trimmed,
        commandId,
      });
      if (res.ok) {
        onCreated(res.data.object_ref.id);
      } else {
        // 保留 name 与 pending（不退出、不清选区）
        setError(res);
      }
    } finally {
      setSubmitting(false);
    }
  }

  const preview =
    selectedText.length > 60 ? `${selectedText.slice(0, 60)}…` : selectedText;

  return (
    <div className="selection-toolbar">
      <style jsx>{`
        .selection-toolbar {
          margin: 0.5rem 0;
          padding: 0.75rem;
          border: 1px solid #adb5bd;
          border-radius: 6px;
          background: #fff;
          display: flex;
          flex-direction: column;
          gap: 0.5rem;
        }
        .selection-toolbar__preview {
          margin: 0;
          font-size: 0.85rem;
          color: #495057;
          line-height: 1.5;
          white-space: pre-wrap;
        }
        .selection-toolbar__name {
          padding: 0.4rem 0.6rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.9rem;
        }
        .selection-toolbar__actions {
          display: flex;
          gap: 0.5rem;
        }
        .selection-toolbar__save {
          padding: 0.4rem 1rem;
          border: 1px solid #37b24d;
          border-radius: 4px;
          background: #37b24d;
          color: #fff;
          cursor: pointer;
        }
        .selection-toolbar__save:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }
        .selection-toolbar__cancel {
          padding: 0.4rem 1rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
        }
        .selection-toolbar__error {
          margin: 0;
          font-size: 0.85rem;
          color: #fa5252;
          line-height: 1.5;
        }
        .selection-toolbar__conflicts {
          margin: 0.25rem 0 0;
          padding-left: 1.2rem;
        }
      `}</style>
      <p className="selection-toolbar__preview">{preview}</p>
      <input
        className="selection-toolbar__name"
        type="text"
        value={name}
        placeholder="输入片段名"
        onChange={(e) => setName(e.target.value)}
      />
      <div className="selection-toolbar__actions">
        <button
          type="button"
          className="selection-toolbar__save"
          disabled={submitting}
          onClick={handleSave}
        >
          保存候选
        </button>
        <button
          type="button"
          className="selection-toolbar__cancel"
          onClick={onCancel}
        >
          取消
        </button>
      </div>
      {error !== null && (
        <div className="selection-toolbar__error" role="alert">
          {error.code === "range_overlap" ? (
            <div>
              <p>该范围与已有片段重叠（不自动裁切）：</p>
              <ul className="selection-toolbar__conflicts">
                {(
                  (error.details?.conflicts ?? []) as Array<{
                    name: string;
                    start: number;
                    end: number;
                  }>
                ).map((c, i) => (
                  <li key={i}>
                    {c.name}（{c.start}–{c.end}）
                  </li>
                ))}
              </ul>
            </div>
          ) : error.code === "revision_conflict" ? (
            <p>范围集合版本已变化，请刷新后重试。</p>
          ) : (
            <p>{error.message}</p>
          )}
        </div>
      )}
    </div>
  );
}
