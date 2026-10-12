"use client";

// SOURCE-14 · 正文变更复核 UI（契约 v2.9，附录 01 §2 S4/S5 + S2）。
//
// 冻结决策（本文件头注记）：
// - 自取数据（与 FragmentActions 同一模式）：useEffect 内
//   getProject(client, projectId) + listSourceRevisions(client, projectId)；
//   取数成功 = 新快照；内部重取（refreshTick）与错误重试（retryTick）
//   都触发同一取数链。
// - 项目级「正文变更」按钮（非行级）：点击 → 展开两步表单（阶段 1：
//   目标版本下拉选择器（选项 = 当前列表中 is_active === false 的源版本，
//   文本 = 版本 id + created_at 日期；当前 active 版本不显示——已是
//   active → 422）+「预览」+「取消」；阶段 2：impact 摘要 +「确认激活」
//   +「取消」）。
// - 「取消」→ 回到按钮态（零 API 调用）。
// - 「预览」：本地校验——目标版本必须已选（未选 → 本地提示「请选择一个
//   目标版本。」，不发请求、保留表单）；否则每次点击生成一次 commandId
//   （makeCommandId()）→ previewSourceActivate(client, projectId,
//   { targetRevisionId, commandId })；成功（res.ok）→ 表单进入阶段 2
//   （展示 impact 摘要 +「确认激活」+「取消」）；失败（!res.ok）→ 行内
//   错误（保留表单，不退出）。
// - 「确认激活」：每次点击生成一次 commandId（makeCommandId()）→
//   applySourceActivate(client, projectId, { previewId:
//   preview.preview_id, commandId,
//   expectedActiveRevisionId: project.active_source_revision_id })；
//   expected_active_revision_id 取当前项目的 active_source_revision_id
//   （真实服务端值；为 null 时不提交）；成功（res.ok）→ 清除行内错误 +
//   内部重取（项目 active_source_revision_id 更新）；失败（!res.ok）→
//   行内错误（保留表单与 impact 摘要，不退出）。
// - 行内错误区（独立于 FragmentActions 的 rowErrorText）：already_active
//   →「该版本已是当前活跃版本。」；not_found →「指定正文版本不存在。」；
//   preview_stale →「预览已过期，请重新预览。」；revision_conflict →
//   「版本已变化，请刷新后重试。」；validation_failed →
//   details.violations 每条 message；其他 → error.message。
// - 表单用 state（reviewing: { open, stage: "preview" | "confirm",
//   targetRevisionId, preview? }）；新取数成功（新快照）时一并清除
//   （防陈旧表单/错误/预览）。
// - 组件不直接 fetch（走 client）；不导航（不跳转页面）；无硬编码占位
//   文本。

import { useEffect, useState } from "react";

import defaultClient from "@/shared/api/client";
import {
  applySourceActivate,
  getProject,
  listSourceRevisions,
  previewSourceActivate,
} from "../api";
import type {
  ProjectDto,
  SourceActivatePreviewDto,
  SourceRevisionListDto,
} from "../api";
import type { StudioApiResponse } from "@/shared/api/types";

export interface SourceChangeReviewProps {
  projectId: string;
}

type DataState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      project: ProjectDto;
      revisions: SourceRevisionListDto["items"];
    };

/** 行内错误（预览/应用共用同一错误区；仅用错误字段，data 形状随动作）。 */
type ReviewError =
  | (StudioApiResponse<SourceActivatePreviewDto> & { ok: false })
  | (StudioApiResponse<ProjectDto> & { ok: false });

/** 两步表单状态（展开态 + 阶段 + 目标版本选择 + 预览结果）。 */
type ReviewingForm = {
  open: boolean;
  stage: "preview" | "confirm";
  targetRevisionId: string;
  preview?: SourceActivatePreviewDto;
};

/** 每次点击生成一次 commandId（显式点击，无自动提交）。 */
function makeCommandId(): string {
  return crypto.randomUUID();
}

/** created_at（Unix 秒）→ YYYY-MM-DD 日期文本。 */
function formatCreatedAt(createdAt: number): string {
  return new Date(createdAt * 1000).toISOString().slice(0, 10);
}

export default function SourceChangeReview({
  projectId,
}: SourceChangeReviewProps) {
  const clientObj = defaultClient;
  const [state, setState] = useState<DataState>({ kind: "loading" });
  const [retryTick, setRetryTick] = useState(0);
  const [refreshTick, setRefreshTick] = useState(0);
  const [reviewing, setReviewing] = useState<ReviewingForm>({
    open: false,
    stage: "preview",
    targetRevisionId: "",
  });
  const [reviewError, setReviewError] = useState<ReviewError | null>(null);
  const [reviewSubmitting, setReviewSubmitting] = useState(false);

  useEffect(() => {
    let alive = true;
    setState({ kind: "loading" });
    void (async () => {
      const projectRes = await getProject(clientObj, projectId);
      if (!alive) return;
      if (!projectRes.ok) {
        setState({ kind: "error", message: projectRes.message });
        return;
      }
      const revisionsRes = await listSourceRevisions(clientObj, projectId);
      if (!alive) return;
      if (!revisionsRes.ok) {
        setState({ kind: "error", message: revisionsRes.message });
        return;
      }
      // 取数成功 = 新快照：两步表单与行内错误属于旧快照，一并清除
      // （防陈旧表单/错误/预览）
      setState({
        kind: "ready",
        project: projectRes.data,
        revisions: revisionsRes.data.items,
      });
      setReviewing({ open: false, stage: "preview", targetRevisionId: "" });
      setReviewError(null);
      setReviewSubmitting(false);
    })();
    return () => {
      alive = false;
    };
  }, [clientObj, projectId, retryTick, refreshTick]);

  /** 点击「正文变更」：展开两步表单（阶段 1：目标版本下拉 + 预览；
   * 初始值空）。 */
  function handleOpen(): void {
    setReviewing({ open: true, stage: "preview", targetRevisionId: "" });
    setReviewError(null);
  }

  /** 点击「取消」：回到按钮态（零 API 调用）。 */
  function handleCancel(): void {
    setReviewing({ open: false, stage: "preview", targetRevisionId: "" });
    setReviewError(null);
  }

  /** 点击「预览」：本地校验（目标版本必须已选，未选不发请求、保留表单）
   * → 每次点击生成一次 commandId → previewSourceActivate。 */
  async function handlePreview(): Promise<void> {
    const targetRevisionId = reviewing.targetRevisionId;
    if (targetRevisionId === "") {
      // 本地拦截：未选不发请求，保留表单
      setReviewError({
        ok: false,
        code: "validation_failed",
        message: "请选择一个目标版本。",
        details: {
          violations: [
            {
              field: "target_revision_id",
              rule: "required",
              message: "请选择一个目标版本。",
            },
          ],
        },
      });
      return;
    }
    setReviewSubmitting(true);
    try {
      // 每次点击生成一次 commandId（显式点击，无自动激活）
      const commandId = makeCommandId();
      const res = await previewSourceActivate(clientObj, projectId, {
        targetRevisionId,
        commandId,
      });
      if (res.ok) {
        // 成功：进入阶段 2（展示 impact 摘要 +「确认激活」+「取消」）
        setReviewError(null);
        setReviewing((prev) => ({
          ...prev,
          stage: "confirm",
          preview: res.data,
        }));
      } else {
        // 失败：保留表单（不退出、不丢错误）
        setReviewError(res);
      }
    } finally {
      setReviewSubmitting(false);
    }
  }

  /** 点击「确认激活」：每次点击生成一次 commandId → applySourceActivate。 */
  async function handleApply(): Promise<void> {
    const preview = reviewing.preview;
    if (preview === undefined) return;
    const project = state.kind === "ready" ? state.project : null;
    // expected_active_revision_id 取当前项目的 active_source_revision_id
    // （真实服务端值；为 null 时不提交）
    const expectedActiveRevisionId = project?.active_source_revision_id ?? null;
    if (expectedActiveRevisionId === null) return;
    setReviewSubmitting(true);
    try {
      // 每次点击生成一次 commandId（显式点击，无自动激活）
      const commandId = makeCommandId();
      const res = await applySourceActivate(clientObj, projectId, {
        previewId: preview.preview_id,
        commandId,
        expectedActiveRevisionId,
      });
      if (res.ok) {
        // 成功：清除行内错误，内部重取（项目 active_source_revision_id 更新）
        setReviewError(null);
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：保留表单与 impact 摘要（不退出、不丢错误）
        setReviewError(res);
      }
    } finally {
      setReviewSubmitting(false);
    }
  }

  /**
   * 行内错误文案（预览/应用统一）：precondition_failed 且
   * reason=already_active →「该版本已是当前活跃版本。」；validation_failed
   * → details.violations 每条 message；not_found →「指定正文版本不存在。」；
   * preview_stale →「预览已过期，请重新预览。」；revision_conflict →
   * 「版本已变化，请刷新后重试。」；其他 → error.message。
   */
  function reviewErrorText(error: ReviewError): string {
    if (error.code === "precondition_failed") {
      const details = error.details as Record<string, unknown> | undefined;
      if (details?.reason === "already_active") {
        return "该版本已是当前活跃版本。";
      }
      return error.message;
    }
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
    if (error.code === "not_found") {
      return "指定正文版本不存在。";
    }
    if (error.code === "preview_stale") {
      return "预览已过期，请重新预览。";
    }
    if (error.code === "revision_conflict") {
      return "版本已变化，请刷新后重试。";
    }
    return error.message;
  }

  let body: React.ReactNode;
  if (state.kind === "loading") {
    body = (
      <p className="source-change-review__status" role="status">
        正在加载正文版本…
      </p>
    );
  } else if (state.kind === "error") {
    body = (
      <div className="source-change-review__status" role="alert">
        <p>正文版本读取失败：{state.message}</p>
        <button
          type="button"
          className="source-change-review__retry"
          onClick={() => setRetryTick((t) => t + 1)}
        >
          重试
        </button>
      </div>
    );
  } else {
    // 项目级「正文变更」动作区（非行级）：目标版本选项 = 当前列表中
    // is_active === false 的源版本（当前 active 版本不显示——已是 active
    // → 422）
    const inactiveRevisions = state.revisions.filter(
      (r) => r.is_active === false,
    );
    body = (
      <div className="source-change-review__ready">
        {reviewing.open ? (
          <div className="source-change-review__form">
            {reviewing.stage === "confirm" &&
              reviewing.preview !== undefined && (
                <p className="source-change-review__impact">
                  影响：{reviewing.preview.impact.affected.length} 项 /
                  待复核：{reviewing.preview.impact.needs_review.length}{" "}
                  项 / 可保留：
                  {reviewing.preview.impact.preservable.length} 项
                </p>
              )}
            {reviewing.stage === "preview" ? (
              <>
                <select
                  className="source-change-review__select"
                  value={reviewing.targetRevisionId}
                  onChange={(e) =>
                    setReviewing((prev) => ({
                      ...prev,
                      targetRevisionId: e.target.value,
                    }))
                  }
                >
                  <option value="">选择目标版本</option>
                  {inactiveRevisions.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.id} · {formatCreatedAt(r.created_at)}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="source-change-review__preview"
                  disabled={reviewSubmitting}
                  onClick={() => void handlePreview()}
                >
                  预览
                </button>
              </>
            ) : (
              <button
                type="button"
                className="source-change-review__apply"
                disabled={reviewSubmitting}
                onClick={() => void handleApply()}
              >
                确认激活
              </button>
            )}
            <button
              type="button"
              className="source-change-review__cancel"
              onClick={() => handleCancel()}
            >
              取消
            </button>
            {reviewError !== null && (
              <div className="source-change-review__error" role="alert">
                <p>{reviewErrorText(reviewError)}</p>
              </div>
            )}
          </div>
        ) : (
          <button
            type="button"
            className="source-change-review__open"
            onClick={() => handleOpen()}
          >
            正文变更
          </button>
        )}
      </div>
    );
  }

  return (
    <section className="source-change-review" aria-label="正文变更">
      <style jsx>{`
        .source-change-review {
          display: block;
        }
        .source-change-review__ready {
          display: block;
        }
        .source-change-review__open {
          padding: 0.2rem 0.7rem;
          border: 1px solid #6741bc;
          border-radius: 4px;
          background: #fff;
          color: #6741bc;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .source-change-review__form {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
          margin-bottom: 0.5rem;
          padding: 0.5rem;
          border: 1px solid #d0b9ff;
          border-radius: 4px;
          background: #f6f2ff;
        }
        .source-change-review__select {
          padding: 0.3rem 0.5rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.85rem;
        }
        .source-change-review__impact {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #495057;
          line-height: 1.5;
        }
        .source-change-review__preview {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #1971c2;
          border-radius: 4px;
          background: #fff;
          color: #1971c2;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .source-change-review__preview:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .source-change-review__apply {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #6741bc;
          border-radius: 4px;
          background: #6741bc;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .source-change-review__apply:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .source-change-review__cancel {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          color: #495057;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .source-change-review__error {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #fa5252;
          line-height: 1.5;
        }
        .source-change-review__status {
          margin: 1rem 0;
          color: #495057;
          line-height: 1.6;
        }
        .source-change-review__retry {
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
