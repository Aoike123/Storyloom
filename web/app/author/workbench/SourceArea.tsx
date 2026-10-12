"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, Plus } from "lucide-react";
import {
  checkRange,
  codePointAt,
  renderSegments,
  utf16ToCodePointOffset,
  type FragmentRange,
  type SourceFragment,
  type SourceModel,
  type SourceView,
} from "./source-model";
import { type Workspace } from "./workspace-state";

interface Props {
  source: SourceModel;
  open: boolean;
  view: SourceView;
  activeFragmentId: string | null;
  active: Workspace;
  candidateFragments: SourceFragment[]; // 前端创建、未持久化的候选片段
  pendingRange: FragmentRange | null; // 划选未标注文字得到的临时区间
  onToggleView: (view: SourceView) => void;
  onCollapse: () => void;
  onJump: (f: SourceFragment) => void;
  onSetPending: (range: FragmentRange | null) => void;
  onAddCandidate?: (range: FragmentRange) => Promise<void>;
  onConfirmFragment?: (fragmentId: string) => Promise<void>;
  onImportStory?: (content: string, title: string) => void;
  importBusy?: boolean;
  importError?: string | null;
}

interface Pt {
  top: number;
  left: number;
}

// 左侧持续来源区：原文连续阅读 + 整块荧光区域；原文/片段两视图共用同一稳定 id。
// A1：点击区域/片段 = 片段跳转（恢复目标片段工作上下文）。
// A2：在原文视图划选“未标注”文字 → 校验连续非空且与已有片段不重叠 → 浮动「新建片段」→
//     创建候选片段（isExample，不写库）；划选/确认/取消分离；冲突时指出已有片段。
export default function SourceArea({
  source,
  open,
  view,
  activeFragmentId,
  active,
  candidateFragments,
  pendingRange,
  onToggleView,
  onCollapse,
  onJump,
  onSetPending,
  onAddCandidate,
  onConfirmFragment,
  onImportStory,
  importBusy,
  importError,
}: Props) {
  const proseRef = useRef<HTMLDivElement>(null);
  const [pendingRect, setPendingRect] = useState<Pt | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftTitle, setDraftTitle] = useState("");

  const fragments = [...source.fragments, ...candidateFragments].sort(
    (a, b) => a.range.start - b.range.start,
  );
  const byId = new Map(fragments.map((f) => [f.id, f]));
  const hasText = source.text.length > 0;
  const isFilm = active === "film";
  const fragmentViewLabel = isFilm ? "片段资源" : "故事片段";
  const activeId = activeFragmentId;
  const segments = renderSegments(source.text, fragments, pendingRange);

  // 用 TreeWalker 按文本节点顺序累计字符，把 DOM 选区映射回正文的真实字符偏移（不用 DOM 坐标/段号）。
  function captureRange(container: HTMLElement): FragmentRange | null {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    if (
      !container.contains(range.startContainer) ||
      !container.contains(range.endContainer)
    )
      return null;
    const offsetOf = (node: Node, nodeOffset: number) => {
      let total = 0;
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
      let n = walker.nextNode();
      while (n) {
        if (n === node) return total + nodeOffset;
        total += n.nodeValue?.length ?? 0;
        n = walker.nextNode();
      }
      return total + nodeOffset;
    };
    // TreeWalker 累计的是 UTF-16 码元偏移；契约单位是码点，换算后再做空白裁剪。
    let start = utf16ToCodePointOffset(source.text, offsetOf(range.startContainer, range.startOffset));
    let end = utf16ToCodePointOffset(source.text, offsetOf(range.endContainer, range.endOffset));
    if (start > end) [start, end] = [end, start];
    while (start < end && /\s/.test(codePointAt(source.text, start) ?? "")) start++;
    while (end > start && /\s/.test(codePointAt(source.text, end - 1) ?? "")) end--;
    return start < end ? { start, end } : null;
  }

  const clearPending = useCallback(() => {
    onSetPending(null);
    setPendingRect(null);
  }, [onSetPending]);

  const handleMouseUp = () => {
    if (view !== "original" || !hasText) return;
    const container = proseRef.current;
    if (!container) return;
    const sel = window.getSelection();
    setConflict(null);
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) {
      clearPending();
      return;
    }
    const r = captureRange(container);
    if (!r) {
      clearPending();
      return;
    }
    const check = checkRange(r, source.text, fragments);
    if (!check.valid) {
      clearPending();
      if (check.reason) setConflict(check.reason); // 与已有片段重叠 → 指出
      return;
    }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    onSetPending(r);
    setPendingRect({
      top: Math.max(8, rect.top - 42),
      left: Math.max(8, Math.min(rect.left, window.innerWidth - 180)),
    });
  };

  // 切视图 / 滚动 / 跳转后清理临时选区，避免固定浮层残留
  useEffect(() => {
    if (view !== "original") clearPending();
  }, [view, clearPending]);

  useEffect(() => {
    if (!conflict) return;
    const t = setTimeout(() => setConflict(null), 3500);
    return () => clearTimeout(t);
  }, [conflict]);

  async function handleCreate() {
    if (!pendingRange || !onAddCandidate) return;
    try {
      await onAddCandidate({ ...pendingRange });
      clearPending();
      window.getSelection()?.removeAllRanges();
    } catch (e) {
      setConflict(e instanceof Error ? e.message : "建立片段失败，请重试。");
    }
  }

  async function handleConfirm(f: SourceFragment) {
    if (!onConfirmFragment) return;
    try {
      await onConfirmFragment(f.id);
    } catch (e) {
      setConflict(e instanceof Error ? e.message : "确认片段失败，请重试。");
    }
  }

  const jumpTo = (f: SourceFragment) => {
    clearPending();
    onJump(f);
  };

  // 打开「导入故事」面板：预填现有正文（便于编辑后重新导入/更换）
  function openImport() {
    setDraft(source.text ?? "");
    setDraftTitle("");
    setConflict(null);
    setImporting(true);
  }

  if (!open) return null;

  return (
    <aside className="workbench-source" aria-label="故事来源">
      <div className="workbench-source-head">
        <div className="workbench-source-title-wrap">
          <span className="workbench-source-title">故事来源</span>
          <small className="workbench-source-version">
            正文 {source.revision}
          </small>
        </div>
        <div className="workbench-source-tools">
          <button
            type="button"
            aria-pressed={view === "original"}
            onClick={() => onToggleView("original")}
          >
            原文
          </button>
          <button
            type="button"
            aria-pressed={view === "fragments"}
            onClick={() => onToggleView("fragments")}
          >
            {fragmentViewLabel}
          </button>
          <button
            type="button"
            className="workbench-source-import"
            title="导入 / 更换故事原文"
            onClick={openImport}
          >
            <Plus size={14} />
            导入
          </button>
          <button
            type="button"
            className="icon"
            aria-label="收起来源区"
            title="收起来源区"
            onClick={onCollapse}
          >
            <ChevronLeft size={15} />
          </button>
        </div>
      </div>

      <div
        className="workbench-source-scroll"
        onScroll={() => pendingRange && clearPending()}
      >
        {importing ? (
          <div className="workbench-import">
            <label className="workbench-import-field">
              <span>故事正文</span>
              <textarea
                className="workbench-import-text"
                rows={12}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="在这里粘贴你的故事原文…"
              />
            </label>
            <label className="workbench-import-field">
              <span>标题（可选）</span>
              <input
                className="workbench-import-title"
                value={draftTitle}
                onChange={(e) => setDraftTitle(e.target.value)}
                placeholder="给这个故事起个标题"
              />
            </label>
            {importError ? (
              <p className="workbench-import-error" role="alert">
                {importError}
              </p>
            ) : null}
            <div className="workbench-import-actions">
              <button
                type="button"
                className="is-primary"
                disabled={importBusy || !draft.trim()}
                onClick={() => {
                  if (!draft.trim()) return;
                  onImportStory?.(draft.trim(), draftTitle.trim());
                  setImporting(false);
                }}
              >
                {importBusy ? "导入中…" : "导入故事"}
              </button>
              <button type="button" onClick={() => setImporting(false)}>
                取消
              </button>
            </div>
          </div>
        ) : (
          <>
            {conflict ? (
              <div className="workbench-conflict" role="status">
                {conflict}
              </div>
            ) : null}
            {!hasText ? (
              <div className="workbench-source-empty">
                <p>
                  这个项目还没有原文。导入你的故事后，就能在这里连续阅读、划选建立片段。
                </p>
                <button
                  type="button"
                  className="is-primary workbench-source-empty-import"
                  onClick={openImport}
                >
                  <Plus size={15} />
                  导入故事
                </button>
              </div>
            ) : view === "original" ? (
              <div
                className="workbench-original"
                ref={proseRef}
                onMouseUp={handleMouseUp}
              >
                {segments.map((seg, i) =>
                  seg.region ? (
                    <span
                      key={i}
                      className={
                        "workbench-region" +
                        (seg.region.kind === "pending" ? " is-pending" : "") +
                        (activeId && seg.region.id === activeId
                          ? " is-active"
                          : "")
                      }
                      role={
                        seg.region.kind === "fragment" ? "button" : undefined
                      }
                      tabIndex={seg.region.kind === "fragment" ? 0 : undefined}
                      onClick={
                        seg.region.kind === "fragment"
                          ? () => {
                              const f = byId.get(seg.region!.id);
                              if (f) jumpTo(f);
                            }
                          : undefined
                      }
                      onKeyDown={
                        seg.region.kind === "fragment"
                          ? (e) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                const f = byId.get(seg.region!.id);
                                if (f) jumpTo(f);
                              }
                            }
                          : undefined
                      }
                    >
                      {seg.text}
                    </span>
                  ) : (
                    <span key={i} className="workbench-gap">
                      {seg.text}
                    </span>
                  ),
                )}
              </div>
            ) : (
              <div className="workbench-fragments">
                {fragments.length === 0 ? (
                  <div className="workbench-source-empty">
                    尚无片段。在「原文」视图划选一段连续文字，建立第一个故事片段。
                  </div>
                ) : (
                  fragments.map((f, i) => (
                    <div
                      key={f.id}
                      className={
                        "workbench-fragment" +
                        (activeId === f.id ? " is-active" : "")
                      }
                    >
                      <button
                        type="button"
                        className="workbench-fragment-main"
                        onClick={() => jumpTo(f)}
                      >
                        <span className="workbench-fragment-index">{i + 1}</span>
                        <span className="workbench-fragment-body">
                          <strong>{f.name}</strong>
                          <small>
                            {f.origin} · {f.progress}
                          </small>
                          <span
                            className={
                              "workbench-fragment-status is-" +
                              (f.status === "已确认" ? "done" : "todo")
                            }
                          >
                            {f.status}
                          </span>
                        </span>
                        {f.isExample ? (
                          <em className="workbench-fragment-example">示例</em>
                        ) : null}
                      </button>
                      {f.status === "待确认" && !f.isExample ? (
                        <button
                          type="button"
                          className="workbench-fragment-confirm"
                          title="确认该片段"
                          onClick={() => handleConfirm(f)}
                        >
                          确认
                        </button>
                      ) : null}
                    </div>
                  ))
                )}
              </div>
            )}
          </>
        )}
      </div>

      <div className="workbench-source-foot">
        {view === "original"
          ? isFilm
            ? "按故事片段取用视频资源"
            : "划选未标注文字可建立新片段"
          : "片段保留原文范围 · 选取与确认分别操作"}
      </div>

      {pendingRange && pendingRect ? (
        <div
          className="workbench-pending-tool"
          style={{ top: pendingRect.top, left: pendingRect.left }}
          role="group"
          aria-label="新建片段"
        >
          <button type="button" className="is-primary" onClick={handleCreate}>
            新建片段
          </button>
          <button type="button" onClick={clearPending}>
            取消
          </button>
        </div>
      ) : null}
    </aside>
  );
}
