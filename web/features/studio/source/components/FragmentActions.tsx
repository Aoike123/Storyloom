"use client";

// SOURCE-14 · 片段确认动作（契约 v2.9，附录 02 §2 F6）。
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
//   动作区仅 state === "candidate" 显示「确认」按钮（F6 前置：只有 candidate
//   可确认；"选择不确认"），其他状态不显示任何确认动作。
// - 确认动作（冻结）：仅显式点击（无自动确认、无 useEffect 自动提交、
//   无默认勾选）；每次点击生成一次 commandId（crypto.randomUUID）；
//   expected_revision = 该行 fragment.object_ref.revision（真实服务端值）；
//   提交中该行确认按钮 disabled（防双击）；成功（res.ok）→ 清除该行错误
//   + 内部重取（列表状态标签更新为已确认）；失败（!res.ok）→ 该行
//   setError(res)，保留行与按钮（不隐藏、不退出）。
// - 行内错误区（仅该片段确认失败时显示）：revision_conflict →
//   「片段版本已变化，请刷新后重试。」；precondition_failed →
//   「片段状态已变化，请刷新后重试。」；其他显示 error.message。
// - 组件不直接 fetch（走 client）；不 import 旧组件；无硬编码占位文本；
//   不导航（不跳转页面）。

import { useEffect, useState } from "react";

import defaultClient from "@/shared/api/client";
import { confirmFragment, fetchAllFragments } from "../api";
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

type ConfirmError = StudioApiResponse<FragmentDto> & { ok: false };

export default function FragmentActions({
  projectId,
  client,
  refreshKey = 0,
}: FragmentActionsProps) {
  const clientObj = client ?? defaultClient;
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [retryTick, setRetryTick] = useState(0);
  const [refreshTick, setRefreshTick] = useState(0);
  const [errors, setErrors] = useState<Record<string, ConfirmError>>({});
  const [submitting, setSubmitting] = useState<Record<string, boolean>>({});

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
      // 新取数成功 = 新快照：行内确认错误属于旧快照，一并清除
      setErrors({});
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
      const commandId = crypto.randomUUID();
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
        setErrors((prev) => ({ ...prev, [fid]: res as ConfirmError }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 行内错误文案：revision_conflict / precondition_failed / 其他 message。 */
  function rowErrorText(error: ConfirmError): string {
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
    <section className="fragment-actions" aria-label="片段确认">
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
