"use client";

// ASSET-02-b · 资产上传 UI（ASSET-02 前端分卡；契约 附录 05 §2 AS5）。
//
// 只读参照（未改动）：backend/studio/assets/{api.py,service.py}（ASSET-02-a，
// 提交 e23da29 已验收）与既有的 SOURCE-12/13/14 前端 api 模块写法
// + web/shared/api/client.ts 的客户端写法。
//
// 冻结决策（本文件头注记，供验收对照）：
// - 完全受控 + 可注入：所有输入来自 props（projectId / assetId /
//   expectedRevision）；本组件**不自取数据**（无副作用钩子、无项目/资产读取），
//   只持有一次上传尝试的瞬时 UI 状态（submitting / pendingRevision / error）
//   与文件选择。`upload` 为可注入的上传函数（默认 defaultUpload 走真实
//   `fetch` multipart），测试注入假实现即可，**绝不打真实网络**。
// - 复用既有 HTTP 客户端的方式：`createClient`（shared/api/client.ts）的
//   `post()` 会把正文交给 JSON 序列化并强制 JSON 内容类型请求头，无法承载
//   multipart/form-data（FormData 会被序列化成 `{}` 且 boundary 丢失），
//   因此**不**经它发正文；但本组件仍复用 shared/api/types 的
//   `StudioApiResponse` / `ApiErrorCode` 形状，并用默认客户端
//   `defaultClient.getToken()` 取 Bearer 令牌（不另造一套鉴权/存储逻辑）。
//   本卡白名单禁止新建 `web/features/studio/assets/api.ts`，故 multipart
//   传输实现内联于此，且保持与 client.ts 一致的状态码→错误码映射与非契约
//   错误体兜底。
// - 接正式上传：选择文件后以 `FormData` 提交到
//   `POST /api/studio/projects/{pid}/assets/{aid}/media`，字段名严格为
//   `file` / `command_id` /（可选）`expected_revision`，**只**放在 multipart
//   body 里（不设内容类型头，交给运行时补 boundary；command_id 不进任何
//   JSON 正文）。
// - 失败保留原版本（核心验收）：422 / 409 / 网络错误一律走
//   `applyUploadResult` 的失败分支——只改 `submitting`（回 false）与
//   `error`（真实错误：status/code/message/details.violations），
//   `baselineRevision`（原版本，来自 props）与 `pendingRevision` 原样保留；
//   绝不把 UI 切到完成/采纳态，错误可重试（重试 = 新的一次尝试）。
// - 不隐式采纳（核心验收）：本组件没有任何采纳/批准语义、文案或调用；
//   `onUploaded` 只在服务端 201（或同 command_id 重放的 200）后回调，且
//   仅表示"新版本待审核"（review_status 原样展示，本组件不落任何采纳状态）。
// - 不自动批准、不跳转、不自动切视图：不使用副作用钩子、不做客户端路由跳转、
//   无自动提交；上传只在显式点击时发生。
// - 加载态：提交中禁用「上传」与「重试」（防重复提交）；每次新尝试都由
//   `submitUpload` 生成**新的** `command_id`（`crypto.randomUUID()`），
//   重试同一文件同样重新生成（不做"同一次尝试复用同一 command_id"）。
// - 图片扩展名边界（MEDIA-01 冻结）：`ALLOWED_EXTENSIONS` 仅
//   mp4/webm/mov/m4a/wav/mp3，**不含**图片扩展名。本组件**不**做前端扩展名
//   白名单前置拦截（不假装"支持图片"、不放宽契约），png/jpg 等会被服务端
//   422 `validation_failed`（violations[].rule = `unsupported_type`）拒绝，
//   UI 原样显示该 errors 文案（含允许列表），并保留原版本。
// - 禁止项：不引用旧产品页面组件与旧阅读视图；不 import 应用层 app 目录；
//   不读写 localStorage 存业务数据（仅经默认客户端读鉴权令牌）。

import { useReducer, useState } from "react";

import defaultClient from "@/shared/api/client";
import type { ApiErrorCode, StudioApiError, StudioApiResponse } from "@/shared/api/types";

// ---------------------------------------------------------------------------
// 契约 DTO（ASSET-02-a 成功体 = 8 键资产 DTO；本卡白名单不新建 assets/api.ts，
// 故此处只声明本组件读取所需字段，逐字对齐 backend/studio/assets/service.py
// `_to_dto_all_versions`）。
// ---------------------------------------------------------------------------

/** 资产版本（current_revision / versions[] 共用键；版本号、媒体、审核状态）。 */
export interface UploadedAssetRevisionDto {
  revision: number;
  spec: Record<string, unknown>;
  media_artifact_id: string | null;
  review_status: string;
}

/** 8 键资产 DTO（上传成功 201 / 幂等重放 200 同形）。 */
export interface UploadedAssetDto {
  object_ref: { kind: string; id: string; revision: number };
  type: string;
  name: string;
  character: { kind: string; id: string } | null;
  current_revision: UploadedAssetRevisionDto;
  versions: Array<UploadedAssetRevisionDto & { created_at: number }>;
  occurrences: unknown[];
  bindings: unknown[];
}

/** 上传结果：复用 shared/api/types 的冻结响应形状（不另造一套）。 */
export type UploadOutcome = StudioApiResponse<UploadedAssetDto>;

/** 一次上传的完整请求（command_id 由 submitUpload 生成后注入）。 */
export interface UploadRequest {
  projectId: string;
  assetId: string;
  file: File;
  commandId: string;
  expectedRevision: number | null;
}

/** 调用方提供的上传输入（无 command_id：每次尝试现生成）。 */
export interface UploadRequestInput {
  projectId: string;
  assetId: string;
  file: File;
  expectedRevision: number | null;
}

/** 可注入的上传实现（默认 defaultUpload）。 */
export type UploadFn = (request: UploadRequest) => Promise<UploadOutcome>;

// ---------------------------------------------------------------------------
// 纯逻辑：路径 / FormData / command_id / 结果状态机（可 vm 真跑，无 React）
// ---------------------------------------------------------------------------

/** AS5 上传路径：POST /api/studio/projects/{pid}/assets/{aid}/media。 */
export function mediaUploadPath(projectId: string, assetId: string): string {
  return `/api/studio/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/media`;
}

/**
 * 组装 multipart 表单，字段名严格为 file / command_id / expected_revision。
 * - `expected_revision` 仅在提供正整数时附加（= 走 CAS）；null/未提供 → 不带
 *   该字段（不伪造 CAS 基线）；非正整数视为"未提供"这一最保守解释；
 * - 只写入 FormData，绝不把这些字段放进 query 或 JSON body。
 */
export function buildUploadFormData(
  file: File,
  commandId: string,
  expectedRevision: number | null,
): FormData {
  const form = new FormData();
  form.append("file", file);
  form.append("command_id", commandId);
  if (
    typeof expectedRevision === "number" &&
    Number.isInteger(expectedRevision) &&
    expectedRevision >= 1
  ) {
    form.append("expected_revision", String(expectedRevision));
  }
  return form;
}

/** 每次尝试现生成一个 command_id（幂等键）。 */
export function newUploadCommandId(): string {
  return crypto.randomUUID();
}

/** 选定上传实现：注入优先，否则真实 multipart 传输。 */
export function resolveUploader(upload?: UploadFn): UploadFn {
  return upload ?? defaultUpload;
}

/** 一次上传尝试（含本次使用的 command_id，便于诊断/测试）。 */
export interface UploadAttempt {
  commandId: string;
  outcome: UploadOutcome;
}

/**
 * 执行一次上传尝试：先现生成 command_id，再交给上传实现。
 * 每次调用（含对同一文件的每次重试）都是新尝试 → 新 command_id。
 */
export async function submitUpload(
  uploader: UploadFn,
  input: UploadRequestInput,
  makeCommandId: () => string = newUploadCommandId,
): Promise<UploadAttempt> {
  const commandId = makeCommandId();
  const outcome = await uploader({ ...input, commandId });
  return { commandId, outcome };
}

/** 归一化后的真实错误（只取错误体字段，不泛化、不吞）。 */
export interface UploadErrorInfo {
  status: number | null;
  code: string;
  message: string;
  violations: string[];
}

/**
 * 单条 violation → 展示文本：`field · rule：message`（三者取服务端原值）。
 * 只保留存在的部分；三者皆空 → null（不造空文案）。
 */
function formatViolation(item: unknown): string | null {
  if (item === null || typeof item !== "object") return null;
  const record = item as { field?: unknown; rule?: unknown; message?: unknown };
  const field = typeof record.field === "string" ? record.field : "";
  const rule = typeof record.rule === "string" ? record.rule : "";
  const message = typeof record.message === "string" ? record.message : "";
  const head = [field, rule].filter((part) => part !== "").join(" · ");
  if (head === "" && message === "") return null;
  if (head === "") return message;
  return message === "" ? head : `${head}：${message}`;
}

/** 从错误结果提取 status / code / message / details.violations（真实错误）。 */
export function normalizeUploadError(outcome: StudioApiError): UploadErrorInfo {
  const violations: string[] = [];
  const rawViolations = outcome.details?.violations;
  if (Array.isArray(rawViolations)) {
    for (const item of rawViolations) {
      const formatted = formatViolation(item);
      if (formatted !== null) violations.push(formatted);
    }
  }
  return {
    status: typeof outcome.status === "number" ? outcome.status : null,
    code: outcome.code,
    message: outcome.message,
    violations,
  };
}

/** 错误展示文案：HTTP 状态 + 错误码 + message + 每条 violation（完整真实错误）。 */
export function formatUploadError(error: UploadErrorInfo): string {
  const head =
    error.status === null
      ? `上传失败（${error.code}）：${error.message}`
      : `上传失败（HTTP ${error.status} · ${error.code}）：${error.message}`;
  if (error.violations.length === 0) return head;
  return `${head} ${error.violations.join(" ")}`;
}

/** 一次上传尝试的 UI 状态（baselineRevision 为原版本，失败路径绝不改写）。 */
export interface UploadState {
  /** 原版本（CAS 基线，来自 props；本模块任何函数都不改它）。 */
  baselineRevision: number | null;
  /** 提交中：true 时禁用重复提交。 */
  submitting: boolean;
  /** 服务端返回的待审核新版本号（仅"待审核"，非完成/采纳态）。 */
  pendingRevision: number | null;
  /** 待审核新版本的 review_status（服务端原值，如 unreviewed）。 */
  pendingReviewStatus: string | null;
  /** 最近一次失败的真实错误；null = 无错误。 */
  error: UploadErrorInfo | null;
}

/** 初态：原版本 = props 基线，其余空。 */
export function createUploadState(baselineRevision: number | null): UploadState {
  return {
    baselineRevision,
    submitting: false,
    pendingRevision: null,
    pendingReviewStatus: null,
    error: null,
  };
}

/** 开始一次提交：置 submitting、清旧错误；原版本与待审核版本不动。 */
export function beginUpload(state: UploadState): UploadState {
  return { ...state, submitting: true, error: null };
}

/** 读取 DTO 的 current_revision（形状异常 → null，不臆造版本号）。 */
function readCurrentRevision(
  data: UploadedAssetDto,
): UploadedAssetRevisionDto | null {
  const current = (data as { current_revision?: unknown }).current_revision;
  if (current === null || typeof current !== "object") return null;
  const revision = (current as { revision?: unknown }).revision;
  if (typeof revision !== "number") return null;
  return current as UploadedAssetRevisionDto;
}

/**
 * 应用一次上传结果（失败保留原版本的核心实现）：
 * - 成功：置待审核新版本（revision/review_status），清错误；baselineRevision
 *   （原版本）与展示中的版本信息一并保留，不表示完成/采纳态；
 * - 失败：只置 error（真实错误）并把 submitting 回 false；
 *   baselineRevision 与 pendingRevision **原样保留**（不清空、不替换），
 *   允许同一失败再次提交（重试 = 新尝试）。
 */
export function applyUploadResult(
  state: UploadState,
  outcome: UploadOutcome,
): UploadState {
  if (outcome.ok) {
    const current = readCurrentRevision(outcome.data);
    if (current === null) {
      // 成功体形状异常：不臆造新版本号，也不清空已展示的版本信息
      return { ...state, submitting: false, error: null };
    }
    return {
      ...state,
      submitting: false,
      pendingRevision: current.revision,
      pendingReviewStatus: current.review_status,
      error: null,
    };
  }
  return { ...state, submitting: false, error: normalizeUploadError(outcome) };
}

/** 组件动作（显式点击驱动，无自动提交）。 */
export type UploadAction =
  | { type: "begin" }
  | { type: "result"; outcome: UploadOutcome }
  | { type: "reset" }
  | { type: "setBaseline"; baselineRevision: number | null };

/** 状态机：begin → result；换文件 reset；props 基线变化 setBaseline。 */
export function uploadReducer(
  state: UploadState,
  action: UploadAction,
): UploadState {
  switch (action.type) {
    case "begin":
      return beginUpload(state);
    case "result":
      return applyUploadResult(state, action.outcome);
    case "reset":
      // 换了上传目标文件：清旧错误与旧待审核提示，原版本不动
      return createUploadState(state.baselineRevision);
    case "setBaseline":
      return state.baselineRevision === action.baselineRevision
        ? state
        : createUploadState(action.baselineRevision);
    default:
      return state;
  }
}

/** 状态对应的状态栏文案（成功只说"待审核"，失败给完整真实错误）。 */
export function uploadStatusText(state: UploadState): string {
  if (state.submitting) return "正在上传…";
  if (state.error !== null) return formatUploadError(state.error);
  if (state.pendingRevision !== null) {
    return `新版本 revision ${state.pendingRevision} 待审核（review_status：${
      state.pendingReviewStatus ?? "未知"
    }）。`;
  }
  return "";
}

// ---------------------------------------------------------------------------
// 默认真实上传实现（multipart fetch；仅此函数调用 fetch）
// ---------------------------------------------------------------------------

/**
 * 与 shared/api/client.ts 同一环境变量来源；未配置时用同源相对路径
 * （next.config.ts 已把 /api/* 反代到后端），从而不硬编码冻结端口。
 */
const UPLOAD_BASE: string =
  (typeof process !== "undefined" &&
    process.env?.NEXT_PUBLIC_STUDIO_API_BASE) ||
  "";

/** 非契约错误体的状态码兜底（与 shared/api/client.ts 的映射保持一致）。 */
function errorCodeForStatus(status: number): ApiErrorCode {
  switch (status) {
    case 401:
      return "unauthenticated";
    case 403:
      return "forbidden";
    case 404:
      return "not_found";
    case 409:
      return "revision_conflict";
    case 422:
      return "validation_failed";
    default:
      return "internal_error";
  }
}

/** 非 2xx：解析冻结错误体 {error:{code,message,details}}，解析失败按状态码兜底。 */
async function readUploadError(res: Response): Promise<UploadOutcome> {
  let code: ApiErrorCode = errorCodeForStatus(res.status);
  let message = `请求失败（HTTP ${res.status}），请重试。`;
  let details: Record<string, unknown> | undefined;
  try {
    const parsed = (await res.json()) as {
      error?: { code?: unknown; message?: unknown; details?: unknown };
    };
    const errObj = parsed?.error;
    if (errObj !== undefined && errObj !== null && typeof errObj === "object") {
      if (typeof errObj.code === "string") code = errObj.code as ApiErrorCode;
      if (typeof errObj.message === "string") message = errObj.message;
      if (errObj.details !== undefined && errObj.details !== null) {
        details = errObj.details as Record<string, unknown>;
      }
    }
  } catch {
    // 非 JSON 错误体：保留状态码兜底文案（不吞状态码）
  }
  const failure: StudioApiError = { ok: false, status: res.status, code, message };
  if (details !== undefined) failure.details = details;
  return failure;
}

/**
 * 默认真实上传：`fetch` + FormData 到
 * POST /api/studio/projects/{pid}/assets/{aid}/media。
 * - 不设内容类型头（运行时按 multipart 边界生成）；
 * - 鉴权令牌复用默认客户端的 getToken（不另造 localStorage 读写）；
 * - 网络异常 → `network`（连接失败），非 2xx → 原样解析错误体透传。
 */
export async function defaultUpload(
  request: UploadRequest,
): Promise<UploadOutcome> {
  const headers: Record<string, string> = {};
  const token = defaultClient.getToken();
  if (token !== null && token !== "") {
    headers["Authorization"] = `Bearer ${token}`;
  }
  try {
    const res = await fetch(UPLOAD_BASE + mediaUploadPath(request.projectId, request.assetId), {
      method: "POST",
      headers,
      body: buildUploadFormData(
        request.file,
        request.commandId,
        request.expectedRevision,
      ),
    });
    if (!res.ok) return await readUploadError(res);
    const data = (await res.json()) as UploadedAssetDto;
    return { ok: true, data };
  } catch {
    return { ok: false, code: "network", message: "连接失败，请重试。" };
  }
}

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------

export interface AssetUploadProps {
  projectId: string;
  assetId: string;
  /** CAS 基线（可选）：提供正整数时随 multipart 提交 expected_revision。 */
  expectedRevision?: number | null;
  /** 上传成功（201 / 幂等重放 200）回调：仅表示新版本待审核。 */
  onUploaded?: (asset: UploadedAssetDto) => void;
  /** 可注入上传实现（测试用假实现替换，绝不打真实网络）。 */
  upload?: UploadFn;
}

export default function AssetUpload({
  projectId,
  assetId,
  expectedRevision,
  onUploaded,
  upload,
}: AssetUploadProps) {
  const baselineRevision =
    typeof expectedRevision === "number" ? expectedRevision : null;
  const [state, dispatch] = useReducer(
    uploadReducer,
    baselineRevision,
    createUploadState,
  );
  const [file, setFile] = useState<File | null>(null);

  // 基线（props）变化 = 父组件换了资产或刷新了原版本：渲染期同步重挂原版本，
  // 并清掉旧尝试的 pending/error（不自动提交、不自动上传）。
  if (state.baselineRevision !== baselineRevision) {
    dispatch({ type: "setBaseline", baselineRevision });
  }

  /** 显式点击「上传」/「重试」：每次都是新尝试（新 command_id）。 */
  async function handleUpload(): Promise<void> {
    if (file === null || state.submitting) return;
    dispatch({ type: "begin" });
    const uploader = resolveUploader(upload);
    try {
      const attempt = await submitUpload(uploader, {
        projectId,
        assetId,
        file,
        expectedRevision: baselineRevision,
      });
      dispatch({ type: "result", outcome: attempt.outcome });
      if (attempt.outcome.ok) onUploaded?.(attempt.outcome.data);
    } catch {
      // 注入实现抛异常：同样保留原版本，显示真实兜底错误（不伪装成功）
      dispatch({
        type: "result",
        outcome: { ok: false, code: "network", message: "上传请求失败，请重试。" },
      });
    }
  }

  const statusText = uploadStatusText(state);

  return (
    <section className="asset-upload" aria-label="资产媒体上传">
      <style jsx>{`
        .asset-upload {
          display: block;
          padding: 0.75rem;
          border: 1px solid #dee2e6;
          border-radius: 6px;
          background: #fff;
          font-size: 0.85rem;
          color: #212529;
        }
        .asset-upload__baseline {
          margin: 0 0 0.5rem;
          color: #495057;
        }
        .asset-upload__picker {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
        }
        .asset-upload__file {
          margin: 0.5rem 0 0;
          color: #343a40;
          word-break: break-all;
        }
        .asset-upload__hint {
          margin: 0.5rem 0 0;
          color: #868e96;
          line-height: 1.5;
        }
        .asset-upload__submit {
          flex-shrink: 0;
          padding: 0.25rem 0.8rem;
          border: 1px solid #1971c2;
          border-radius: 4px;
          background: #1971c2;
          color: #fff;
          cursor: pointer;
        }
        .asset-upload__submit:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .asset-upload__status {
          margin: 0.5rem 0 0;
          color: #495057;
        }
        .asset-upload__error {
          margin: 0.5rem 0 0;
          color: #fa5252;
          line-height: 1.5;
        }
        .asset-upload__error p {
          margin: 0 0 0.25rem;
        }
        .asset-upload__retry {
          padding: 0.2rem 0.7rem;
          border: 1px solid #fa5252;
          border-radius: 4px;
          background: #fff;
          color: #fa5252;
          cursor: pointer;
        }
        .asset-upload__retry:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
      `}</style>
      <p className="asset-upload__baseline">
        原版本：revision{" "}
        {state.baselineRevision === null ? "未提供" : state.baselineRevision}
      </p>
      <div className="asset-upload__picker">
        <input
          className="asset-upload__input"
          type="file"
          aria-label="选择媒体文件"
          disabled={state.submitting}
          onChange={(e) => {
            const chosen = e.target.files?.[0] ?? null;
            setFile(chosen);
            // 换文件 = 新的一次尝试目标：清掉旧错误与旧待审核提示
            dispatch({ type: "reset" });
          }}
        />
        <button
          type="button"
          className="asset-upload__submit"
          disabled={file === null || state.submitting}
          onClick={() => void handleUpload()}
        >
          上传
        </button>
      </div>
      {file !== null && (
        <p className="asset-upload__file">已选择文件：{file.name}</p>
      )}
      <p className="asset-upload__hint">
        媒体类型由服务端校验（mp4、webm、mov、m4a、wav、mp3）；其它文件会被服务端拒绝，并在此原样显示真实错误。
      </p>
      {state.error !== null ? (
        <div className="asset-upload__error" role="alert">
          <p>{statusText}</p>
          <button
            type="button"
            className="asset-upload__retry"
            disabled={state.submitting || file === null}
            onClick={() => void handleUpload()}
          >
            重试
          </button>
        </div>
      ) : statusText !== "" ? (
        <p className="asset-upload__status" role="status">
          {statusText}
        </p>
      ) : null}
    </section>
  );
}
