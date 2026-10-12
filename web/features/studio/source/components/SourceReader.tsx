"use client";

// SOURCE-12 · 连续原文阅读视图（服务端 canonical 文本连续排布 + 真实片段荧光标记）。
//
// 冻结决策（本文件头注记）：
// - 片段数据组件自取，不接受外部 fragments prop：加载链
//   P3 → active_source_revision_id → S3 全文，随后 F2 跟随 next_cursor
//   全量翻页（经 api.ts fetchAllFragments，与 FragmentList 共用同一实现）
//   仅为高亮段取有效 state；refreshKey 变化 → 整条链重取。
// - 三态分开：加载中 / 错误（真实 message + 重试按钮，绝不渲染为空故事）/
//   空态（active_source_revision_id 为空 → “尚未导入正文”，不伪造任何内容）。
// - 正文流内只有 <span>（普通段）与 <mark>（高亮段）：无卡片/标题/状态文字
//   插入，荧光区域即唯一标记；<mark> 锚点 id = `studio-fragment-${fragmentId}`
//   （真实服务端 ID，供 FragmentList 定位），class 含有效状态
//   （候选/已确认/待复核/已退役四态视觉可区分；retired 不进正文高亮，
//   样式仅为防御保留）。
// - 无自动样例：一切正文文本来自服务端 canonical_content。

import { useCallback, useEffect, useRef, useState } from "react";

import defaultClient from "@/shared/api/client";
import {
  computeSegments,
  fetchAllFragments,
  getProject,
  getSourceRevision,
  selectionToRange,
} from "../api";
import type { FragmentDto, NodeAnchor, StudioClient } from "../api";
import { validateRange } from "../ranges";
import SelectionToolbar from "./SelectionToolbar";

export interface SourceReaderProps {
  projectId: string;
  client?: StudioClient;
  refreshKey?: number;
  onFragmentClick?: (fragmentId: string) => void;
}

type ReaderState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | {
      kind: "ready";
      canonical: string;
      fragments: FragmentDto[];
      sourceRevisionId: string;
    };

/**
 * 把原生 DOM 选区锚点（Node + offset）换算为 NodeAnchor（相对正文容器
 * .source-reader__text 的 children 顺序）。
 * - anchor === container（容器级锚点）：offset 为子元素索引；
 *   offset ∈ [0, children.length] → { nodeIndex: offset, offset: 0 }
 *   （offset === children.length 即文本末尾）；否则 null；
 * - anchor 是 Text 且 container.contains(anchor)：parent 必须是 container 的
 *   直接子元素；nodeIndex = container.children 中 parent 的索引；
 *   offset ∈ [0, anchor.data.length] → { nodeIndex, offset }；否则 null；
 * - 其余 → null。
 */
function domAnchorToNodeAnchor(
  container: HTMLElement,
  anchor: Node,
  offset: number,
): NodeAnchor | null {
  if (anchor === container) {
    if (
      Number.isInteger(offset) &&
      offset >= 0 &&
      offset <= container.children.length
    ) {
      return { nodeIndex: offset, offset: 0 };
    }
    return null;
  }
  if (anchor.nodeType === 3 && container.contains(anchor)) {
    const parent = anchor.parentElement;
    if (parent === null || parent.parentElement !== container) return null;
    const nodeIndex = Array.from(container.children).indexOf(parent);
    const text = anchor as Text;
    if (Number.isInteger(offset) && offset >= 0 && offset <= text.data.length) {
      return { nodeIndex, offset };
    }
    return null;
  }
  return null;
}

export default function SourceReader({
  projectId,
  client,
  refreshKey = 0,
  onFragmentClick,
}: SourceReaderProps) {
  const clientObj = client ?? defaultClient;
  const [state, setState] = useState<ReaderState>({ kind: "loading" });
  const [retryTick, setRetryTick] = useState(0);
  const [pending, setPending] = useState<{
    start: number;
    end: number;
    text: string;
    commandId: string;
  } | null>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    setState({ kind: "loading" });
    // 重取即清 pending（防陈旧工具条）；pending 不在依赖数组，不会引起重取循环
    setPending(null);
    void (async () => {
      const projectRes = await getProject(clientObj, projectId);
      if (!alive) return;
      if (!projectRes.ok) {
        setState({ kind: "error", message: projectRes.message });
        return;
      }
      const revisionId = projectRes.data.active_source_revision_id;
      if (!revisionId) {
        // 空态：项目尚无活跃来源版本——诚实文案，不伪造任何内容
        setState({ kind: "empty" });
        return;
      }
      const revisionRes = await getSourceRevision(clientObj, projectId, revisionId);
      if (!alive) return;
      if (!revisionRes.ok) {
        setState({ kind: "error", message: revisionRes.message });
        return;
      }
      // F2 全量翻页仅为高亮段取 state（retired 由 computeSegments 排除出正文流）
      const fragmentsRes = await fetchAllFragments(clientObj, projectId);
      if (!alive) return;
      if (!fragmentsRes.ok) {
        setState({ kind: "error", message: fragmentsRes.message });
        return;
      }
      setState({
        kind: "ready",
        canonical: revisionRes.data.canonical_content,
        fragments: fragmentsRes.data,
        sourceRevisionId: revisionId,
      });
    })();
    return () => {
      alive = false;
    };
  }, [clientObj, projectId, refreshKey, retryTick]);

  const retry = useCallback(() => setRetryTick((t) => t + 1), []);

  // 原生 DOM 选区 → pending 态（仅 ready 时监听 selectionchange）。
  // 依赖 state：state 变化（重取）时重注册，handler 闭包取最新 canonical/fragments。
  // 该 effect 不做任何重取（重取只由 refreshKey/retryTick/created 触发），
  // 不会引起重取循环。
  useEffect(() => {
    if (state.kind !== "ready") return;
    const handler = () => {
      const sel = document.getSelection();
      // 工具条内焦点（如点击输入框）→ 保持 pending，不清除
      const activeInToolbar =
        document.activeElement != null &&
        toolbarRef.current != null &&
        toolbarRef.current.contains(document.activeElement);
      if (!sel || sel.rangeCount === 0) {
        if (activeInToolbar) return;
        setPending(null);
        return;
      }
      const range = sel.getRangeAt(0);
      if (range.collapsed) {
        if (activeInToolbar) return;
        setPending(null);
        return;
      }
      const container = textRef.current;
      if (!container) {
        setPending(null);
        return;
      }
      // 选区两端必须落在正文容器内（容器级或被容器包含）
      if (
        range.startContainer !== container &&
        !container.contains(range.startContainer)
      ) {
        setPending(null);
        return;
      }
      if (
        range.endContainer !== container &&
        !container.contains(range.endContainer)
      ) {
        setPending(null);
        return;
      }
      const startA = domAnchorToNodeAnchor(
        container,
        range.startContainer,
        range.startOffset,
      );
      const endA = domAnchorToNodeAnchor(
        container,
        range.endContainer,
        range.endOffset,
      );
      if (startA === null || endA === null) {
        setPending(null);
        return;
      }
      const segments = computeSegments(state.canonical, state.fragments);
      const texts = segments.map((s) => state.canonical.slice(s.start, s.end));
      const rng = selectionToRange(texts, startA, endA);
      if (rng === null) {
        setPending(null);
        return;
      }
      // 空白/越界选区不出现工具条（不提交）
      if (validateRange(state.canonical, rng.start, rng.end) !== "ok") {
        setPending(null);
        return;
      }
      setPending({
        start: rng.start,
        end: rng.end,
        text: state.canonical.slice(rng.start, rng.end),
        commandId: crypto.randomUUID(),
      });
    };
    document.addEventListener("selectionchange", handler);
    return () => {
      document.removeEventListener("selectionchange", handler);
    };
  }, [state]);

  let body: React.ReactNode;
  if (state.kind === "loading") {
    body = (
      <p className="source-reader__status" role="status">
        正在加载正文…
      </p>
    );
  } else if (state.kind === "error") {
    body = (
      <div className="source-reader__status" role="alert">
        <p>正文读取失败：{state.message}</p>
        <button
          type="button"
          className="source-reader__retry"
          onClick={retry}
        >
          重试
        </button>
      </div>
    );
  } else if (state.kind === "empty") {
    body = <p className="source-reader__status">尚未导入正文。</p>;
  } else {
    const segments = computeSegments(state.canonical, state.fragments);
    body = (
      <>
        <div className="source-reader__text" ref={textRef}>
          {segments.map((seg) => {
            const text = state.canonical.slice(seg.start, seg.end);
            if (seg.fragmentId === null) {
              return <span key={`plain-${seg.start}-${seg.end}`}>{text}</span>;
            }
            const fragmentId = seg.fragmentId;
            return (
              <mark
                key={fragmentId}
                id={`studio-fragment-${fragmentId}`}
                data-fragment-id={fragmentId}
                className={`source-reader__fragment source-reader__fragment--${seg.state ?? "unknown"}`}
                onClick={
                  onFragmentClick ? () => onFragmentClick(fragmentId) : undefined
                }
              >
                {text}
              </mark>
            );
          })}
        </div>
        {pending !== null && (
          <div ref={toolbarRef}>
            <SelectionToolbar
              projectId={projectId}
              sourceRevisionId={state.sourceRevisionId}
              range={{ start: pending.start, end: pending.end }}
              selectedText={pending.text}
              commandId={pending.commandId}
              onCancel={() => setPending(null)}
              onCreated={(fid) => {
                setPending(null);
                setRetryTick((t) => t + 1);
              }}
              client={clientObj}
            />
          </div>
        )}
      </>
    );
  }

  return (
    <section className="source-reader" aria-label="原文">
      <style jsx>{`
        .source-reader {
          display: block;
        }
        .source-reader__text {
          white-space: pre-wrap;
          word-break: break-word;
          font-size: 1rem;
          line-height: 1.9;
          color: #1a1a1a;
        }
        .source-reader__fragment {
          color: inherit;
          border-radius: 2px;
          padding: 0 1px;
        }
        .source-reader__fragment--candidate {
          background: #fff3bf;
        }
        .source-reader__fragment--confirmed {
          background: #d3f9d8;
        }
        .source-reader__fragment--pending_review {
          background: #ffe8cc;
        }
        .source-reader__fragment--retired {
          background: #e9ecef;
          text-decoration: line-through;
        }
        .source-reader__fragment.studio-fragment--flash {
          animation: studio-fragment-flash 1.2s ease-out;
        }
        @keyframes studio-fragment-flash {
          0% {
            outline: 2px solid #f08c00;
            outline-offset: 1px;
          }
          100% {
            outline: 2px solid transparent;
            outline-offset: 1px;
          }
        }
        .source-reader__status {
          margin: 1rem 0;
          color: #495057;
          line-height: 1.6;
        }
        .source-reader__retry {
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
