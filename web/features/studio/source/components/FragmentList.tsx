"use client";

// SOURCE-12 · 动态片段列表（F2 真实数据，与阅读视图共用真实服务端 ID）。
//
// 冻结决策（本文件头注记）：
// - F2 拉取默认全部 state（不传 state 过滤），经 api.ts fetchAllFragments
//   跟随 next_cursor 全量翻页（keyset，limit=200 为服务端 clamp 上限，
//   与 SourceReader 共用同一实现）；refreshKey 变化 → 重取。
// - 三态分开：加载中 / 错误（真实 message + 重试按钮）/ 空态
//   （“还没有片段”诚实文案，与错误态严格分开）。
// - 状态标签真实来自服务端 DTO 的有效 state：candidate=候选 /
//   confirmed=已确认 / pending_review=待复核 / retired=已退役；
//   未知值原样显示，不本地编造。
// - 列表项点击定位：先调 onLocate（若给），再执行缺省行为——
//   document.getElementById(`studio-fragment-${id}`)?.scrollIntoView
//   （smooth/center）并短暂加闪烁 class（setTimeout 移除，class 名与
//   SourceReader 的样式约定一致）。
// - 退役片段在正文中没有高亮范围（computeSegments 排除），定位按钮禁用
//   （按钮态诚实，title 说明原因）。

import { useEffect, useState } from "react";

import defaultClient from "@/shared/api/client";
import { fetchAllFragments } from "../api";
import type { FragmentDto, StudioClient } from "../api";

const STATE_LABELS: Record<string, string> = {
  candidate: "候选",
  confirmed: "已确认",
  pending_review: "待复核",
  retired: "已退役",
};

/** 有效状态 → 中文标签；未知状态原样显示（不编造）。 */
export function fragmentStateLabel(state: string): string {
  return STATE_LABELS[state] ?? state;
}

/** 定位闪烁 class（与 SourceReader 样式约定一致）。 */
const FLASH_CLASS = "studio-fragment--flash";
const FLASH_MS = 1200;

export interface FragmentListProps {
  projectId: string;
  client?: StudioClient;
  refreshKey?: number;
  onLocate?: (fragmentId: string) => void;
}

type ListState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; items: FragmentDto[] };

export default function FragmentList({
  projectId,
  client,
  refreshKey = 0,
  onLocate,
}: FragmentListProps) {
  const clientObj = client ?? defaultClient;
  const [state, setState] = useState<ListState>({ kind: "loading" });
  const [retryTick, setRetryTick] = useState(0);

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
    })();
    return () => {
      alive = false;
    };
  }, [clientObj, projectId, refreshKey, retryTick]);

  function locate(fragment: FragmentDto): void {
    const fragmentId = fragment.object_ref.id;
    if (onLocate) onLocate(fragmentId);
    const el = document.getElementById(`studio-fragment-${fragmentId}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add(FLASH_CLASS);
    window.setTimeout(() => el.classList.remove(FLASH_CLASS), FLASH_MS);
  }

  let body: React.ReactNode;
  if (state.kind === "loading") {
    body = (
      <p className="fragment-list__status" role="status">
        正在加载片段…
      </p>
    );
  } else if (state.kind === "error") {
    body = (
      <div className="fragment-list__status" role="alert">
        <p>片段列表读取失败：{state.message}</p>
        <button
          type="button"
          className="fragment-list__retry"
          onClick={() => setRetryTick((t) => t + 1)}
        >
          重试
        </button>
      </div>
    );
  } else if (state.items.length === 0) {
    // 空态 ≠ 读失败：诚实文案，不伪造任何片段
    body = <p className="fragment-list__status">还没有片段。</p>;
  } else {
    body = (
      <ul className="fragment-list__items">
        {state.items.map((fragment) => {
          const fragmentId = fragment.object_ref.id;
          const locatable = fragment.state !== "retired";
          return (
            <li
              key={fragmentId}
              className={`fragment-list__item fragment-list__item--${fragment.state}`}
            >
              <span className="fragment-list__name">{fragment.name}</span>
              <span
                className={`fragment-list__state fragment-list__state--${fragment.state}`}
              >
                {fragmentStateLabel(fragment.state)}
              </span>
              <button
                type="button"
                className="fragment-list__locate"
                disabled={!locatable}
                title={
                  locatable
                    ? "在正文中定位该片段"
                    : "已退役片段在正文中没有可定位范围"
                }
                onClick={() => locate(fragment)}
              >
                定位
              </button>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <section className="fragment-list" aria-label="片段列表">
      <style jsx>{`
        .fragment-list {
          display: block;
        }
        .fragment-list__items {
          list-style: none;
          margin: 0;
          padding: 0;
        }
        .fragment-list__item {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          padding: 0.45rem 0;
          border-bottom: 1px solid #e9ecef;
        }
        .fragment-list__name {
          flex: 1;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          color: #1a1a1a;
        }
        .fragment-list__state {
          flex-shrink: 0;
          font-size: 0.8rem;
          padding: 0.1rem 0.5rem;
          border-radius: 999px;
        }
        .fragment-list__state--candidate {
          background: #fff3bf;
          color: #5f3f00;
        }
        .fragment-list__state--confirmed {
          background: #d3f9d8;
          color: #1b4d24;
        }
        .fragment-list__state--pending_review {
          background: #ffe8cc;
          color: #7a3e00;
        }
        .fragment-list__state--retired {
          background: #e9ecef;
          color: #868e96;
        }
        .fragment-list__item--retired .fragment-list__name {
          color: #868e96;
        }
        .fragment-list__locate {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-list__locate:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-list__status {
          margin: 1rem 0;
          color: #495057;
          line-height: 1.6;
        }
        .fragment-list__retry {
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
