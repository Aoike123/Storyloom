"use client";

// SOURCE-14 · 片段确认 + 改名动作（契约 v2.9，附录 02 §2 F6/F4）。
//
// 冻结决策（本文件头注记）：
// - 自取片段数据（与 FragmentList/SourceReader 同一模式）：
//   fetchAllFragments(client ?? defaultClient, projectId)；refreshKey 变化重取；
//   成功内部重取（refreshTick）与错误重试（retryTick）都触发同一取数链。
// - 三态分开（文案与 FragmentList 一致）：加载中「正在加载片段…」（role=status）/
//   错误「片段列表读取失败：{message}」+ 重试按钮（role=alert）/
//   空态「还没有片段。」（active 版本无片段 ≠ 读失败）。
// - 每片段行（列表，按服务端返回序）：片段名 + 状态中文标签（候选/已确认/
//   待复核/已退役，与 FragmentList 一致，经 fragmentStateLabel 复用）；
//   动作区：state === "candidate" 显示「确认」按钮（F6 前置：只有 candidate
//   可确认；"选择不确认"）；state === "candidate" 或 state === "confirmed"
//   显示「改名」按钮（retired 不显示——退役片段不再参与生产，无需改名；
//   F2 列表只列 active 版本绑定片段，pending_review 不会出现在列表中）。
// - 确认动作（SOURCE-14-a 冻结，本卡不变）：仅显式点击（无自动确认、
//   无 useEffect 自动提交、无默认勾选）；每次点击生成一次 commandId
//   （crypto.randomUUID）；expected_revision = 该行 fragment.object_ref.revision
//   （真实服务端值）；提交中该行确认按钮 disabled（防双击）；成功（res.ok）→
//   清除该行错误 + 内部重取（列表状态标签更新为已确认）；失败（!res.ok）→
//   该行 setError(res)，保留行与按钮（不隐藏、不退出）。
// - 改名动作（F4，本卡）：点击「改名」→ 该行动作区切换为行内改名表单
//   （输入框初始值 = 当前 fragment.name +「保存」+「取消」）；「取消」→
//   回到按钮态（零 API 调用）；「保存」：name.trim() === "" → 本地提示
//   （不发请求，保留表单），否则每次点击生成一次 commandId →
//   renameFragment(client, pid, fid, { name: name.trim(),
//   expectedRevision: object_ref.revision, commandId })；提交中该行「保存」
//   按钮 disabled（防双击）；成功（res.ok）→ 清除该行错误 + 内部重取
//   （列表名称更新为服务端 trimmed 值，表单收起）；失败（!res.ok）→ 该行
//   行内错误（保留输入与表单，不退出、不丢错误）。
// - 行内错误区（仅该片段确认/改名失败时显示，两者统一）：validation_failed →
//   显示 details.violations 每条 message（如「片段名不能为空。」/
//   「片段名最长 120 个字符。」）；revision_conflict →
//   「片段版本已变化，请刷新后重试。」；precondition_failed →
//   「片段状态已变化，请刷新后重试。」；其他显示 error.message。
// - 每行的改名表单展开态与输入值用行级 state（renaming:
//   Record<string, { open, value }>），与 14-a 的 errors/submitting 并列；
//   新取数成功（新快照）时一并清除（防陈旧表单/错误）。
// - 组件不直接 fetch（走 client）；不 import 旧组件；无硬编码占位文本；
//   不导航（不跳转页面）。

import { useEffect, useState } from "react";

import defaultClient from "@/shared/api/client";
import {
  confirmFragment,
  fetchAllFragments,
  renameFragment,
} from "../api";
import type { FragmentDto, StudioClient } from "../api";
import type { StudioApiResponse } from "@/shared/api/types";
import { fragmentStateLabel } from "./FragmentList";

export interface FragmentActionsProps {
  projectId: string;
  client?: StudioClient;
  refreshKey?: number;
}

type ListState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; items: FragmentDto[] };

/** 行内错误（确认与改名共用同一错误区）。 */
type RowError = StudioApiResponse<FragmentDto> & { ok: false };

/** 每行改名表单状态（展开态 + 输入值）。 */
type RenamingForm = { open: boolean; value: string };

/** 每次点击生成一次 commandId（显式点击，无自动提交）。 */
function makeCommandId(): string {
  return crypto.randomUUID();
}

export default function FragmentActions({
  projectId,
  client,
  refreshKey = 0,
}: FragmentActionsProps) {
  const clientObj = client ?? defaultClient;
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [retryTick, setRetryTick] = useState(0);
  const [refreshTick, setRefreshTick] = useState(0);
  const [errors, setErrors] = useState<Record<string, RowError>>({});
  const [submitting, setSubmitting] = useState<Record<string, boolean>>({});
  const [renaming, setRenaming] = useState<Record<string, RenamingForm>>({});

  useEffect(() => {
    let alive = true;
    setState({ kind: "loading" });
    void (async () => {
      const res = await fetchAllFragments(clientObj, projectId);
      if (!alive) return;
      if (!res.ok) {
        setState({ kind: "error", message: res.message });
        return;
      }
      setState({ kind: "ready", items: res.data });
      // 新取数成功 = 新快照：行内确认/改名错误与改名表单属于旧快照，一并清除
      setErrors({});
      setRenaming({});
    })();
    return () => {
      alive = false;
    };
  }, [clientObj, projectId, refreshKey, retryTick, refreshTick]);

  async function handleConfirm(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动确认）
      const commandId = makeCommandId();
      const res = await confirmFragment(clientObj, projectId, fid, {
        expectedRevision: fragment.object_ref.revision,
        commandId,
      });
      if (res.ok) {
        // 成功：清除该行错误，内部重取（状态标签更新为已确认）
        setErrors((prev) => {
          const next = { ...prev };
          delete next[fid];
          return next;
        });
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：保留行与按钮（不隐藏、不退出）
        setErrors((prev) => ({ ...prev, [fid]: res as RowError }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「改名」：该行动作区切换为行内改名表单（初始值 = 当前名称）。 */
  function handleRenameOpen(fragment: FragmentDto): void {
    const fid = fragment.object_ref.id;
    setRenaming((prev) => ({
      ...prev,
      [fid]: { open: true, value: fragment.name },
    }));
  }

  /** 点击「取消」：回到按钮态（零 API 调用）。 */
  function handleRenameCancel(fid: string): void {
    setRenaming((prev) => ({
      ...prev,
      [fid]: { open: false, value: "" },
    }));
  }

  async function handleRenameSave(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    const name = (renaming[fid]?.value ?? "").trim();
    if (name === "") {
      // 本地拦截：空名不发请求，保留表单（文案与服务端 rule=required 一致）
      setErrors((prev) => ({
        ...prev,
        [fid]: {
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
        },
      }));
      return;
    }
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动提交）
      const commandId = makeCommandId();
      const res = await renameFragment(clientObj, projectId, fid, {
        name,
        expectedRevision: fragment.object_ref.revision,
        commandId,
      });
      if (res.ok) {
        // 成功：清除该行错误，内部重取（名称更新为服务端 trimmed 值，
        // 新快照清除 renaming → 表单收起）
        setErrors((prev) => {
          const next = { ...prev };
          delete next[fid];
          return next;
        });
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：保留输入与表单（不退出、不丢错误）
        setErrors((prev) => ({ ...prev, [fid]: res as RowError }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /**
   * 行内错误文案（确认与改名共用）：validation_failed → violations 每条
   * message；revision_conflict / precondition_failed → 固定文案；其他 message。
   */
  function rowErrorText(error: RowError): string {
    if (error.code === "validation_failed") {
      const details = error.details as Record<string, unknown> | undefined;
      const violations = (details?.violations ?? []) as Array<{
        message?: string;
      }>;
      const messages = violations
        .map((v) => v.message)
        .filter((m): m is string => typeof m === "string" && m !== "");
      if (messages.length > 0) return messages.join(" ");
      return error.message;
    }
    if (error.code === "revision_conflict") {
      return "片段版本已变化，请刷新后重试。";
    }
    if (error.code === "precondition_failed") {
      return "片段状态已变化，请刷新后重试。";
    }
    return error.message;
  }

  let body: React.ReactNode;
  if (state.kind === "loading") {
    body = (
      <p className="fragment-actions__status" role="status">
        正在加载片段…
      </p>
    );
  } else if (state.kind === "error") {
    body = (
      <div className="fragment-actions__status" role="alert">
        <p>片段列表读取失败：{state.message}</p>
        <button
          type="button"
          className="fragment-actions__retry"
          onClick={() => setRetryTick((t) => t + 1)}
        >
          重试
        </button>
      </div>
    );
  } else if (state.items.length === 0) {
    // 空态 ≠ 读失败：诚实文案，不伪造任何片段
    body = <p className="fragment-actions__status">还没有片段。</p>;
  } else {
    body = (
      <ul className="fragment-actions__items">
        {state.items.map((fragment) => {
          const fid = fragment.object_ref.id;
          const error = errors[fid];
          const isSubmitting = submitting[fid] === true;
          const isCandidate = fragment.state === "candidate";
          // 改名仅 candidate/confirmed（retired 不显示；pending_review
          // 不会出现在 F2 列表中）
          const canRename =
            fragment.state === "candidate" || fragment.state === "confirmed";
          const renameForm = renaming[fid];
          const isRenaming = renameForm?.open === true;
          return (
            <li
              key={fid}
              className={`fragment-actions__item fragment-actions__item--${fragment.state}`}
            >
              <span className="fragment-actions__name">{fragment.name}</span>
              <span
                className={`fragment-actions__state fragment-actions__state--${fragment.state}`}
              >
                {fragmentStateLabel(fragment.state)}
              </span>
              {isRenaming ? (
                <div className="fragment-actions__rename-form">
                  <input
                    className="fragment-actions__rename-input"
                    value={renameForm?.value ?? ""}
                    onChange={(e) =>
                      setRenaming((prev) => ({
                        ...prev,
                        [fid]: { open: true, value: e.target.value },
                      }))
                    }
                  />
                  <button
                    type="button"
                    className="fragment-actions__rename-save"
                    disabled={isSubmitting}
                    onClick={() => void handleRenameSave(fragment)}
                  >
                    保存
                  </button>
                  <button
                    type="button"
                    className="fragment-actions__rename-cancel"
                    onClick={() => handleRenameCancel(fid)}
                  >
                    取消
                  </button>
                </div>
              ) : (
                <>
                  {isCandidate && (
                    <button
                      type="button"
                      className="fragment-actions__confirm"
                      disabled={isSubmitting}
                      onClick={() => void handleConfirm(fragment)}
                    >
                      确认
                    </button>
                  )}
                  {canRename && (
                    <button
                      type="button"
                      className="fragment-actions__rename"
                      onClick={() => handleRenameOpen(fragment)}
                    >
                      改名
                    </button>
                  )}
                </>
              )}
              {error !== undefined && (
                <div className="fragment-actions__row-error" role="alert">
                  <p>{rowErrorText(error)}</p>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <section className="fragment-actions" aria-label="片段动作">
      <style jsx>{`
        .fragment-actions {
          display: block;
        }
        .fragment-actions__items {
          list-style: none;
          margin: 0;
          padding: 0;
        }
        .fragment-actions__item {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
          padding: 0.45rem 0;
          border-bottom: 1px solid #e9ecef;
        }
        .fragment-actions__name {
          flex: 1;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          color: #1a1a1a;
        }
        .fragment-actions__state {
          flex-shrink: 0;
          font-size: 0.8rem;
          padding: 0.1rem 0.5rem;
          border-radius: 999px;
        }
        .fragment-actions__state--candidate {
          background: #fff3bf;
          color: #5f3f00;
        }
        .fragment-actions__state--confirmed {
          background: #d3f9d8;
          color: #1b4d24;
        }
        .fragment-actions__state--pending_review {
          background: #ffe8cc;
          color: #7a3e00;
        }
        .fragment-actions__state--retired {
          background: #e9ecef;
          color: #868e96;
        }
        .fragment-actions__item--retired .fragment-actions__name {
          color: #868e96;
        }
        .fragment-actions__confirm {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #37b24d;
          border-radius: 4px;
          background: #37b24d;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__confirm:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__rename {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #1971c2;
          border-radius: 4px;
          background: #fff;
          color: #1971c2;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__rename-form {
          flex-basis: 100%;
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
          margin-top: 0.25rem;
        }
        .fragment-actions__rename-input {
          flex: 1;
          min-width: 8rem;
          padding: 0.3rem 0.5rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.85rem;
        }
        .fragment-actions__rename-save {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #2f9e44;
          border-radius: 4px;
          background: #2f9e44;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__rename-save:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__rename-cancel {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__row-error {
          flex-basis: 100%;
          margin: 0.25rem 0 0;
          font-size: 0.85rem;
          color: #fa5252;
          line-height: 1.5;
        }
        .fragment-actions__status {
          margin: 1rem 0;
          color: #495057;
          line-height: 1.6;
        }
        .fragment-actions__retry {
          margin-top: 0.5rem;
          padding: 0.35rem 1rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
        }
      `}</style>
      {body}
    </section>
  );
}
