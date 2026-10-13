"use client";

// SOURCE-14 · 片段确认 + 改名 + 退役动作（契约 v2.9，附录 02 §2 F6/F4/F7）。
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
//   F2 列表只列 active 版本绑定片段，pending_review 不会出现在列表中）；
//   state === "candidate" 或 state === "confirmed" 显示「退役」按钮
//   （retired 不显示——F7 前置已 retired → 422；F2 列表只列 active 版本
//   绑定片段）。
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
// - 退役动作（F7，本卡）：两步显式操作（先预览影响、再确认退役），
//   点击「退役」→ 该行动作区切换为行内退役表单（阶段 1：「预览影响」+
//   「取消」）；「取消」→ 回到按钮态（零 API 调用）；「预览影响」：每次
//   点击生成一次 commandId → previewRetireFragment(client, pid, fid,
//   { targetFragmentId: fid, commandId })；成功（res.ok）→ 该行进入阶段 2
//   （展示 impact 摘要 +「确认退役」+「取消」）；失败（!res.ok）→ 行内错误
//   （保留表单，不退出）。「确认退役」：每次点击生成一次 commandId →
//   applyRetireFragment(client, pid, fid, { previewId: res.data.preview_id,
//   commandId, expectedRevision: object_ref.revision,
//   expectedRangeSetRevision: res.data.baseline.range_set.cas ?? 0 })；
//   expected_range_set_revision 取 preview 返回的 baseline.range_set.cas
//   （冻结基准，preview 时快照）；成功（res.ok）→ 清除该行错误 + 内部重取
//   （列表状态标签更新为已退役，该行不再显示确认/改名/退役按钮）；
//   失败（!res.ok）→ 行内错误（保留表单与 impact 摘要，不退出）。
// - 边界动作（F8，本卡）：两步显式操作（先输入新范围预览、再确认调整），
//   点击「边界」→ 该行动作区切换为行内边界表单（阶段 1：两个数字输入框
//   （新 start/新 end，初始值 = 当前 fragment.range.start/end）+「预览」+
//   「取消」）；「取消」→ 回到按钮态（零 API 调用）；「预览」：本地整数
//   校验（Number.isInteger，非整数/NaN/空 → 本地提示「范围端点必须为整数。」，
//   不发请求、保留表单），否则每次点击生成一次 commandId →
//   previewBoundaryFragment(client, pid, fid, { targetFragmentId: fid,
//   newRange: { start, end }, commandId })；成功（res.ok）→ 该行进入阶段 2
//   （展示 impact 摘要 +「确认调整」+「取消」）；失败（!res.ok）→ 行内错误
//   （保留表单，不退出）。「确认调整」：每次点击生成一次 commandId →
//   applyBoundaryFragment(client, pid, fid, { previewId: res.data.preview_id,
//   commandId, expectedRevision: object_ref.revision,
//   expectedRangeSetRevision: res.data.baseline.range_set.cas ?? 0 })；
//   expected_range_set_revision 取 preview 返回的 baseline.range_set.cas
//   （冻结基准，preview 时快照）；成功（res.ok）→ 清除该行错误 + 内部重取
//   （列表范围更新为新范围、revision+1）；失败（!res.ok）→ 行内错误
//   （保留表单与 impact 摘要，不退出）。
//   「边界」按钮仅 state === "candidate" 或 state === "confirmed" 显示
//   （retired 不显示——退役片段不再参与生产，无需调边界；与 14-b/c 同冻结）。
// - 行内错误区（确认/改名/退役/边界统一）：range_invalid → 显示
//   error.message（含 canonical 长度，如「范围为空：end 必须大于 start
//   （正文长度 N 个 UTF-16 单位）。」）；range_overlap → 列出
//   details.conflicts 每个冲突（name + 范围 start/end），文案「该范围与已有
//   片段重叠（不自动裁切）：」（冲突可定位）；preview_stale →
//   「预览已过期，请重新预览。」；revision_conflict → 确认/改名
//   「片段版本已变化，请刷新后重试。」、退役/边界「版本已变化，请刷新后重试。」；
//   precondition_failed → 「片段状态已变化，请刷新后重试。」；
//   validation_failed → 显示 details.violations 每条 message（如「片段名
//   不能为空。」/「片段名最长 120 个字符。」）；其他显示 error.message。
// - 每行的改名表单展开态与输入值用行级 state（renaming:
//   Record<string, { open, value }>）；每行的退役表单用行级 state
//   （retiring: Record<string, { open, stage: "preview" | "confirm",
//   preview? }>）；每行的边界表单用行级 state（boundary:
//   Record<string, { open, stage: "preview" | "confirm",
//   start: number | null, end: number | null, preview? }>），
//   与 14-a/b/c 的 errors/submitting 并列；新取数成功（新快照）时一并
//   清除（防陈旧表单/错误/预览）。
// - 拆分动作（F9，本卡）：两步显式操作（先输入拆分点预览、再确认拆分），
//   点击「拆分」→ 该行动作区切换为行内拆分表单（阶段 1：拆分点数字输入框
//   （初始值 = Math.floor((fragment.range.start + fragment.range.end) / 2)，
//   即当前范围中点）+ 左名/右名可选文本输入框（初始值空）+「预览」+
//   「取消」）；「取消」→ 回到按钮态（零 API 调用）；「预览」：本地校验
//   （split_point 必须为整数（Number.isInteger，非整数/NaN/空 → 本地提示
//   「拆分点必须为整数。」，不发请求、保留表单）；split_point 必须严格介于
//   当前 range 端点之间（fragment.range.start < split_point <
//   fragment.range.end，否则某半空 → 本地提示「拆分点必须严格介于当前范围
//   端点之间。」，不发请求、保留表单）），否则每次点击生成一次 commandId →
//   previewSplitFragment(client, pid, fid, { targetFragmentId: fid,
//   splitPoint, leftName: leftName.trim() === "" ? undefined :
//   leftName.trim(), rightName: rightName.trim() === "" ? undefined :
//   rightName.trim(), commandId })（left_name/right_name 空串 → undefined，
//   不附加进 body）；成功（res.ok）→ 该行进入阶段 2（展示 impact 摘要 +
//   「确认拆分」+「取消」）；失败（!res.ok）→ 行内错误（保留表单，不退出）。
//   「确认拆分」：每次点击生成一次 commandId → applySplitFragment(client,
//   pid, fid, { previewId: res.data.preview_id, commandId,
//   expectedRevision: object_ref.revision,
//   expectedRangeSetRevision: res.data.baseline.range_set.cas ?? 0 })；
//   expected_range_set_revision 取 preview 返回的 baseline.range_set.cas
//   （冻结基准，preview 时快照）；成功（res.ok）→ 清除该行错误 + 内部重取
//   （列表中原片段退役、左/右新片段出现）；失败（!res.ok）→ 行内错误
//   （保留表单与 impact 摘要，不退出）。
//   「拆分」按钮仅 state === "candidate" 或 state === "confirmed" 显示
//   （retired 不显示——F9 前置已 retired → 422；与 14-b/c/d 同冻结）。
// - 每行的拆分表单用行级 state（splitting:
//   Record<string, { open, stage: "preview" | "confirm", splitPoint,
//   leftName, rightName, preview? }>），与 14-a/b/c/d 的
//   errors/submitting/renaming/retiring/boundary 并列；新取数成功（新快照）
//   时一并清除（防陈旧表单/错误/预览）。
// - 合并动作（F10，本卡）：项目级（非行级）两步显式操作（先选择两个片段
//   预览、再确认合并），片段列表上方的项目级动作区：点击「合并」→ 展开
//   项目级合并表单（阶段 1：两个片段选择器（下拉框，选项 = 当前列表中
//   state === "candidate" 或 state === "confirmed" 的片段，选项文本 =
//   片段名 + 状态标签，分别选 a 和 b）+ 可选文本输入框（合并名
//   merged_name，初始值空）+「预览」+「取消」）；「取消」→ 回到按钮态
//   （零 API 调用）；「预览」：本地校验（两个选择器必须各选一个片段且
//   a ≠ b，未选/相同 → 本地提示「请选择两个不同的片段。」，不发请求、
//   保留表单），否则每次点击生成一次 commandId →
//   previewMergeFragments(client, pid, { fragmentIds: [aId, bId],
//   mergedName: mergedName.trim() === "" ? undefined : mergedName.trim(),
//   commandId })（merged_name 空串 → undefined，不附加进 body）；
//   成功（res.ok）→ 进入阶段 2（展示 impact 摘要 +「确认合并」+「取消」）；
//   失败（!res.ok）→ 行内错误（保留表单，不退出）。「确认合并」：每次
//   点击生成一次 commandId → applyMergeFragments(client, pid,
//   { previewId: res.data.preview_id, commandId,
//   expectedRevisionA: fragmentA.object_ref.revision,
//   expectedRevisionB: fragmentB.object_ref.revision,
//   expectedRangeSetRevision: res.data.baseline.range_set.cas ?? 0 })；
//   expected_revision_a/b 取 a/b 片段的 object_ref.revision（真实服务端
//   值）；expected_range_set_revision 取 preview 返回的
//   baseline.range_set.cas（冻结基准，preview 时快照）；成功（res.ok）→
//   清除行内错误 + 内部重取（列表中 a/b 退役、合并新片段出现）；失败
//   （!res.ok）→ 行内错误（保留表单与 impact 摘要，不退出）。
// - 项目级合并表单用 state（merging: { open, stage: "preview" | "confirm",
//   fragmentAId, fragmentBId, mergedName, preview? }），与 14-a/b/c/d/e 的
//   行级 state 并列；新取数成功（新快照）时一并清除（防陈旧表单/错误/
//   预览）。
// - 制作范围（F11/F12，本卡 14-h）：项目级区块（放片段列表上方，与
//   「合并」同层），独立于候选创建（本组件不含任何候选创建调用）。自取数据：片段取数链
//   （fetchAllFragments）成功后顺带 getCurrentProductionScope(client,
//   projectId)；404 且 error.code === "not_found" → 空态「尚未提交
//   制作范围。」（不算错误态）；其他失败 → 行内错误「制作范围读取
//   失败：{message}」+ 重试按钮（retryTick 重走整条取数链）。当前范围
//   展示（有范围时）：covered === true 显示「完整覆盖正文」；否则显示
//   「范围外段落」清单（每段 [start, end) 与长度 end - start）；
//   「覆盖片段清单」列出 fragment_ids 逐个映射当前片段列表中的片段名
//   （id 在当前列表找不到时直接显示 id），并显示片段数量。提交表单：
//   项目级按钮「提交制作范围」→ 展开表单：复选框清单（选项仅为当前
//   列表中 state === "confirmed" 的片段，候选/退役不出现——独立于
//   候选创建；默认全选）+「全部已确认片段」开关（开启 = 不勾选具体
//   片段，提交时省略 fragment_ids）+「提交」+「取消」。本地校验：关闭
//   「全部」开关且勾选为零 → 本地提示「请至少选择一个已确认片段。」，
//   不发请求、保留表单。「取消」→ 回到按钮态（零 API 调用）。「提交」：
//   每次点击 makeCommandId() 恰一次 → createProductionScope(client, pid,
//   { fragmentIds: all ? undefined : 勾选的 id 列表, commandId })；提交中
//   「提交」按钮 disabled（防双击）；成功（res.ok，201 首发与 200 幂等
//   重放同等处理）→ 清除行内错误与表单 + 内部重取（refreshTick，片段 +
//   当前范围）；失败（!res.ok）→ 行内错误（保留表单与勾选，不退出）。
//   行内错误区：duplicate_scope →「相同片段覆盖的制作范围已提交过，历史
//   只追加不重复。」；precondition_failed → error.message；
//   validation_failed → details.violations 每条 message；其他 →
//   error.message。
// - 项目级制作范围表单用 state（scoping: { open, all, selected:
//   Record<string, boolean>, submitting, error? }），与 14-a/b/c/d/e/f
//   的 errors/submitting/renaming/retiring/boundary/splitting/merging
//   并列；新取数成功（新快照）时一并清除（防陈旧表单/错误）。
// - 组件不直接 fetch（走 client）；不 import 旧组件；无硬编码占位文本；
//   不导航（不跳转页面）。

import { useEffect, useState } from "react";

import defaultClient from "@/shared/api/client";
import {
  applyBoundaryFragment,
  applyMergeFragments,
  applyRetireFragment,
  applySplitFragment,
  confirmFragment,
  createProductionScope,
  fetchAllFragments,
  getCurrentProductionScope,
  previewBoundaryFragment,
  previewMergeFragments,
  previewRetireFragment,
  previewSplitFragment,
  renameFragment,
} from "../api";
import type {
  BoundaryPreviewDto,
  FragmentDto,
  MergeApplyDto,
  MergePreviewDto,
  ProductionScopeDto,
  RetirePreviewDto,
  SplitApplyDto,
  SplitPreviewDto,
  StudioClient,
} from "../api";
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

/** 行内错误（确认/改名/退役/边界/拆分/合并共用同一错误区；仅用错误字段，data 形状随动作）。 */
type RowError =
  | (StudioApiResponse<FragmentDto> & { ok: false })
  | (StudioApiResponse<RetirePreviewDto> & { ok: false })
  | (StudioApiResponse<BoundaryPreviewDto> & { ok: false })
  | (StudioApiResponse<SplitPreviewDto> & { ok: false })
  | (StudioApiResponse<SplitApplyDto> & { ok: false })
  | (StudioApiResponse<MergePreviewDto> & { ok: false })
  | (StudioApiResponse<MergeApplyDto> & { ok: false });

/** 产生该行错误的动作（决定 revision_conflict 文案）。 */
type RowErrorSource =
  | "confirm"
  | "rename"
  | "retire"
  | "boundary"
  | "split"
  | "merge";

/** 每行行内错误（错误体 + 产生动作）。 */
type RowErrorEntry = { error: RowError; source: RowErrorSource };

/** 每行改名表单状态（展开态 + 输入值）。 */
type RenamingForm = { open: boolean; value: string };

/** 每行退役表单状态（展开态 + 阶段 + 预览结果）。 */
type RetiringForm = {
  open: boolean;
  stage: "preview" | "confirm";
  preview?: RetirePreviewDto;
};

/** 每行边界表单状态（展开态 + 阶段 + 新范围输入值 + 预览结果）。 */
type BoundaryForm = {
  open: boolean;
  stage: "preview" | "confirm";
  start: number | null;
  end: number | null;
  preview?: BoundaryPreviewDto;
};

/** 每行拆分表单状态（展开态 + 阶段 + 拆分点/左右名输入值 + 预览结果）。 */
type SplittingForm = {
  open: boolean;
  stage: "preview" | "confirm";
  splitPoint: number | null;
  leftName: string;
  rightName: string;
  preview?: SplitPreviewDto;
};

/** 项目级合并表单状态（展开态 + 阶段 + 两个片段选择 + 合并名输入值 + 预览结果）。 */
type MergingForm = {
  open: boolean;
  stage: "preview" | "confirm";
  fragmentAId: string;
  fragmentBId: string;
  mergedName: string;
  preview?: MergePreviewDto;
};

/** 制作范围行内错误（F11/F12；仅错误分支，含本地校验构造的 validation_failed）。 */
type ScopeError = StudioApiResponse<ProductionScopeDto> & { ok: false };

/** 项目级制作范围表单状态（展开态 + 「全部已确认片段」开关 + 逐片段勾选 +
 * 提交态 + 行内错误）。 */
type ScopingForm = {
  open: boolean;
  all: boolean;
  selected: Record<string, boolean>;
  submitting: boolean;
  error?: ScopeError;
};

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
  const [errors, setErrors] = useState<Record<string, RowErrorEntry>>({});
  const [submitting, setSubmitting] = useState<Record<string, boolean>>({});
  const [renaming, setRenaming] = useState<Record<string, RenamingForm>>({});
  const [retiring, setRetiring] = useState<Record<string, RetiringForm>>({});
  const [boundary, setBoundary] = useState<Record<string, BoundaryForm>>({});
  const [splitting, setSplitting] = useState<Record<string, SplittingForm>>({});
  const [merging, setMerging] = useState<MergingForm>({
    open: false,
    stage: "preview",
    fragmentAId: "",
    fragmentBId: "",
    mergedName: "",
  });
  const [mergeError, setMergeError] = useState<RowErrorEntry | null>(null);
  const [mergeSubmitting, setMergeSubmitting] = useState(false);
  // 制作范围（F12 当前范围 / F11 提交表单）：null = 尚未提交（空态，不算
  // 错误态）；scopeError 仅存 404 not_found 之外的读取失败
  const [scope, setScope] = useState<ProductionScopeDto | null>(null);
  const [scopeError, setScopeError] = useState<ScopeError | null>(null);
  const [scoping, setScoping] = useState<ScopingForm>({
    open: false,
    all: true,
    selected: {},
    submitting: false,
  });

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
      // 新取数成功 = 新快照：行内确认/改名/退役/边界/拆分/合并错误、
      // 改名/退役/边界/拆分/合并表单属于旧快照，一并清除（防陈旧表单/错误/预览）
      setErrors({});
      setRenaming({});
      setRetiring({});
      setBoundary({});
      setSplitting({});
      setMerging({
        open: false,
        stage: "preview",
        fragmentAId: "",
        fragmentBId: "",
        mergedName: "",
      });
      setMergeError(null);
      setMergeSubmitting(false);
      // 新快照一并清除制作范围表单（防陈旧表单/错误）
      setScoping({ open: false, all: true, selected: {}, submitting: false });
      // 片段取数链成功后顺带 F12 当前范围（404 not_found = 空态，不算错误）
      const scopeRes = await getCurrentProductionScope(clientObj, projectId);
      if (!alive) return;
      if (scopeRes.ok) {
        setScope(scopeRes.data);
        setScopeError(null);
      } else if (scopeRes.status === 404 && scopeRes.code === "not_found") {
        // 尚未提交制作范围（空态，不算错误态）
        setScope(null);
        setScopeError(null);
      } else {
        setScope(null);
        setScopeError(scopeRes);
      }
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
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "confirm" },
        }));
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
          error: {
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
          source: "rename",
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
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "rename" },
        }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「退役」：该行动作区切换为行内退役表单（阶段 1：预览影响）。 */
  function handleRetireOpen(fragment: FragmentDto): void {
    const fid = fragment.object_ref.id;
    setRetiring((prev) => ({
      ...prev,
      [fid]: { open: true, stage: "preview" },
    }));
  }

  /** 点击「取消」：回到按钮态（零 API 调用）。 */
  function handleRetireCancel(fid: string): void {
    setRetiring((prev) => ({
      ...prev,
      [fid]: { open: false, stage: "preview" },
    }));
  }

  /** 点击「预览影响」：每次点击生成一次 commandId → previewRetireFragment。 */
  async function handleRetirePreview(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动退役）
      const commandId = makeCommandId();
      const res = await previewRetireFragment(clientObj, projectId, fid, {
        targetFragmentId: fid,
        commandId,
      });
      if (res.ok) {
        // 成功：进入阶段 2（展示 impact 摘要 +「确认退役」+「取消」）
        setRetiring((prev) => ({
          ...prev,
          [fid]: { open: true, stage: "confirm", preview: res.data },
        }));
      } else {
        // 失败：保留表单（不退出、不丢错误）
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "retire" },
        }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「确认退役」：每次点击生成一次 commandId → applyRetireFragment。 */
  async function handleRetireApply(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    const preview = retiring[fid]?.preview;
    if (preview === undefined) return;
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动退役）
      const commandId = makeCommandId();
      const res = await applyRetireFragment(clientObj, projectId, fid, {
        previewId: preview.preview_id,
        commandId,
        expectedRevision: fragment.object_ref.revision,
        expectedRangeSetRevision: preview.baseline.range_set.cas ?? 0,
      });
      if (res.ok) {
        // 成功：清除该行错误，内部重取（状态标签更新为已退役，
        // 新快照清除 retiring → 该行不再显示确认/改名/退役按钮）
        setErrors((prev) => {
          const next = { ...prev };
          delete next[fid];
          return next;
        });
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：保留表单与 impact 摘要（不退出、不丢错误）
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "retire" },
        }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「边界」：该行动作区切换为行内边界表单（阶段 1：预览；
   * 初始值 = 当前 fragment.range.start/end）。 */
  function handleBoundaryOpen(fragment: FragmentDto): void {
    const fid = fragment.object_ref.id;
    setBoundary((prev) => ({
      ...prev,
      [fid]: {
        open: true,
        stage: "preview",
        start: fragment.range.start,
        end: fragment.range.end,
      },
    }));
  }

  /** 点击「取消」：回到按钮态（零 API 调用）。 */
  function handleBoundaryCancel(fid: string): void {
    setBoundary((prev) => ({
      ...prev,
      [fid]: { open: false, stage: "preview", start: null, end: null },
    }));
  }

  /** 更新边界表单某个数字输入（空串 → null；否则 Number 化，
   * 非整数/NaN 由「预览」时本地校验拦截）。 */
  function updateBoundaryInput(fid: string, field: "start" | "end", raw: string): void {
    setBoundary((prev) => {
      const form = prev[fid] ?? {
        open: true,
        stage: "preview" as const,
        start: null,
        end: null,
      };
      return {
        ...prev,
        [fid]: { ...form, [field]: raw === "" ? null : Number(raw) },
      };
    });
  }

  /** 点击「预览」：本地整数校验（Number.isInteger，非整数/NaN/空不发请求、
   * 保留表单）→ 每次点击生成一次 commandId → previewBoundaryFragment。 */
  async function handleBoundaryPreview(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    const start = boundary[fid]?.start ?? null;
    const end = boundary[fid]?.end ?? null;
    if (
      start === null ||
      end === null ||
      !Number.isInteger(start) ||
      !Number.isInteger(end)
    ) {
      // 本地拦截：非整数/NaN/空不发请求，保留表单（文案与服务端
      // rule=non_integer 一致）
      setErrors((prev) => ({
        ...prev,
        [fid]: {
          error: {
            ok: false,
            code: "validation_failed",
            message: "范围端点必须为整数。",
            details: {
              violations: [
                {
                  field: "new_range",
                  rule: "non_integer",
                  message: "范围端点必须为整数。",
                },
              ],
            },
          },
          source: "boundary",
        },
      }));
      return;
    }
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动调整）
      const commandId = makeCommandId();
      const res = await previewBoundaryFragment(clientObj, projectId, fid, {
        targetFragmentId: fid,
        newRange: { start, end },
        commandId,
      });
      if (res.ok) {
        // 成功：进入阶段 2（展示 impact 摘要 +「确认调整」+「取消」）
        setBoundary((prev) => ({
          ...prev,
          [fid]: { open: true, stage: "confirm", start, end, preview: res.data },
        }));
      } else {
        // 失败：保留表单（不退出、不丢错误）
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "boundary" },
        }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「确认调整」：每次点击生成一次 commandId → applyBoundaryFragment。 */
  async function handleBoundaryApply(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    const preview = boundary[fid]?.preview;
    if (preview === undefined) return;
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动调整）
      const commandId = makeCommandId();
      const res = await applyBoundaryFragment(clientObj, projectId, fid, {
        previewId: preview.preview_id,
        commandId,
        expectedRevision: fragment.object_ref.revision,
        expectedRangeSetRevision: preview.baseline.range_set.cas ?? 0,
      });
      if (res.ok) {
        // 成功：清除该行错误，内部重取（列表范围更新为新范围、revision+1）
        setErrors((prev) => {
          const next = { ...prev };
          delete next[fid];
          return next;
        });
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：保留表单与 impact 摘要（不退出、不丢错误）
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "boundary" },
        }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「拆分」：该行动作区切换为行内拆分表单（阶段 1：预览；
   * 初始拆分点 = Math.floor((start+end)/2)，即当前范围中点）。 */
  function handleSplitOpen(fragment: FragmentDto): void {
    const fid = fragment.object_ref.id;
    setSplitting((prev) => ({
      ...prev,
      [fid]: {
        open: true,
        stage: "preview",
        splitPoint: Math.floor((fragment.range.start + fragment.range.end) / 2),
        leftName: "",
        rightName: "",
      },
    }));
  }

  /** 点击「取消」：回到按钮态（零 API 调用）。 */
  function handleSplitCancel(fid: string): void {
    setSplitting((prev) => ({
      ...prev,
      [fid]: {
        open: false,
        stage: "preview",
        splitPoint: null,
        leftName: "",
        rightName: "",
      },
    }));
  }

  /** 更新拆分表单某个输入（拆分点：空串 → null；否则 Number 化，
   * 非整数/NaN 由「预览」时本地校验拦截；左右名为文本直存）。 */
  function updateSplitInput(
    fid: string,
    field: "splitPoint" | "leftName" | "rightName",
    raw: string,
  ): void {
    setSplitting((prev) => {
      const form = prev[fid] ?? {
        open: true,
        stage: "preview" as const,
        splitPoint: null,
        leftName: "",
        rightName: "",
      };
      const value =
        field === "splitPoint"
          ? raw === ""
            ? (null as number | null)
            : Number(raw)
          : raw;
      return {
        ...prev,
        [fid]: { ...form, [field]: value },
      };
    });
  }

  /** 点击「预览」：本地校验（Number.isInteger + 严格介于当前 range 端点
   * 之间，非整数/NaN/空/端点/越界不发请求、保留表单）→ 每次点击生成
   * 一次 commandId → previewSplitFragment。 */
  async function handleSplitPreview(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    const splitPoint = splitting[fid]?.splitPoint ?? null;
    const leftName = splitting[fid]?.leftName ?? "";
    const rightName = splitting[fid]?.rightName ?? "";
    if (splitPoint === null || !Number.isInteger(splitPoint)) {
      // 本地拦截：非整数/NaN/空不发请求，保留表单（文案与服务端
      // rule=non_integer 一致）
      setErrors((prev) => ({
        ...prev,
        [fid]: {
          error: {
            ok: false,
            code: "validation_failed",
            message: "拆分点必须为整数。",
            details: {
              violations: [
                {
                  field: "split_point",
                  rule: "non_integer",
                  message: "拆分点必须为整数。",
                },
              ],
            },
          },
          source: "split",
        },
      }));
      return;
    }
    if (!(fragment.range.start < splitPoint && splitPoint < fragment.range.end)) {
      // 本地拦截：不严格介于端点之间（== start / == end / 越界 → 某半空）
      // 不发请求，保留表单（文案与服务端 rule=empty 一致）
      setErrors((prev) => ({
        ...prev,
        [fid]: {
          error: {
            ok: false,
            code: "range_invalid",
            message: "拆分点必须严格介于当前范围端点之间。",
            details: { rule: "empty" },
          },
          source: "split",
        },
      }));
      return;
    }
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动拆分）
      const commandId = makeCommandId();
      const res = await previewSplitFragment(clientObj, projectId, fid, {
        targetFragmentId: fid,
        splitPoint,
        leftName: leftName.trim() === "" ? undefined : leftName.trim(),
        rightName: rightName.trim() === "" ? undefined : rightName.trim(),
        commandId,
      });
      if (res.ok) {
        // 成功：进入阶段 2（展示 impact 摘要 +「确认拆分」+「取消」）
        setSplitting((prev) => ({
          ...prev,
          [fid]: {
            open: true,
            stage: "confirm",
            splitPoint,
            leftName,
            rightName,
            preview: res.data,
          },
        }));
      } else {
        // 失败：保留表单（不退出、不丢错误）
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "split" },
        }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「确认拆分」：每次点击生成一次 commandId → applySplitFragment。 */
  async function handleSplitApply(fragment: FragmentDto): Promise<void> {
    const fid = fragment.object_ref.id;
    const preview = splitting[fid]?.preview;
    if (preview === undefined) return;
    setSubmitting((prev) => ({ ...prev, [fid]: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动拆分）
      const commandId = makeCommandId();
      const res = await applySplitFragment(clientObj, projectId, fid, {
        previewId: preview.preview_id,
        commandId,
        expectedRevision: fragment.object_ref.revision,
        expectedRangeSetRevision: preview.baseline.range_set.cas ?? 0,
      });
      if (res.ok) {
        // 成功：清除该行错误，内部重取（原片段退役、左/右新片段出现）
        setErrors((prev) => {
          const next = { ...prev };
          delete next[fid];
          return next;
        });
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：保留表单与 impact 摘要（不退出、不丢错误）
        setErrors((prev) => ({
          ...prev,
          [fid]: { error: res as RowError, source: "split" },
        }));
      }
    } finally {
      setSubmitting((prev) => ({ ...prev, [fid]: false }));
    }
  }

  /** 点击「合并」：展开项目级合并表单（阶段 1：两个片段选择器 +
   * 合并名输入 + 预览；初始值空）。 */
  function handleMergeOpen(): void {
    setMerging({
      open: true,
      stage: "preview",
      fragmentAId: "",
      fragmentBId: "",
      mergedName: "",
    });
  }

  /** 点击「取消」：回到按钮态（零 API 调用）。 */
  function handleMergeCancel(): void {
    setMerging({
      open: false,
      stage: "preview",
      fragmentAId: "",
      fragmentBId: "",
      mergedName: "",
    });
    setMergeError(null);
  }

  /** 点击「预览」：本地校验（两个选择器必须各选一个片段且 a ≠ b，
   * 未选/相同不发请求、保留表单）→ 每次点击生成一次 commandId →
   * previewMergeFragments。 */
  async function handleMergePreview(): Promise<void> {
    const aId = merging.fragmentAId;
    const bId = merging.fragmentBId;
    const mergedName = merging.mergedName;
    if (aId === "" || bId === "" || aId === bId) {
      // 本地拦截：未选/相同不发请求，保留表单
      setMergeError({
        error: {
          ok: false,
          code: "validation_failed",
          message: "请选择两个不同的片段。",
          details: {
            violations: [
              {
                field: "fragment_ids",
                rule: "cardinality",
                message: "请选择两个不同的片段。",
              },
            ],
          },
        },
        source: "merge",
      });
      return;
    }
    setMergeSubmitting(true);
    try {
      // 每次点击生成一次 commandId（显式点击，无自动合并）
      const commandId = makeCommandId();
      const res = await previewMergeFragments(clientObj, projectId, {
        fragmentIds: [aId, bId],
        mergedName: mergedName.trim() === "" ? undefined : mergedName.trim(),
        commandId,
      });
      if (res.ok) {
        // 成功：进入阶段 2（展示 impact 摘要 +「确认合并」+「取消」）
        setMergeError(null);
        setMerging((prev) => ({
          ...prev,
          stage: "confirm",
          preview: res.data,
        }));
      } else {
        // 失败：保留表单（不退出、不丢错误）
        setMergeError({ error: res as RowError, source: "merge" });
      }
    } finally {
      setMergeSubmitting(false);
    }
  }

  /** 点击「确认合并」：每次点击生成一次 commandId → applyMergeFragments。 */
  async function handleMergeApply(): Promise<void> {
    const preview = merging.preview;
    if (preview === undefined) return;
    const items = state.kind === "ready" ? state.items : [];
    const fragmentA = items.find((f) => f.object_ref.id === merging.fragmentAId);
    const fragmentB = items.find((f) => f.object_ref.id === merging.fragmentBId);
    if (fragmentA === undefined || fragmentB === undefined) return;
    setMergeSubmitting(true);
    try {
      // 每次点击生成一次 commandId（显式点击，无自动合并）
      const commandId = makeCommandId();
      const res = await applyMergeFragments(clientObj, projectId, {
        previewId: preview.preview_id,
        commandId,
        expectedRevisionA: fragmentA.object_ref.revision,
        expectedRevisionB: fragmentB.object_ref.revision,
        expectedRangeSetRevision: preview.baseline.range_set.cas ?? 0,
      });
      if (res.ok) {
        // 成功：清除行内错误，内部重取（列表中 a/b 退役、合并新片段出现）
        setMergeError(null);
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：保留表单与 impact 摘要（不退出、不丢错误）
        setMergeError({ error: res as RowError, source: "merge" });
      }
    } finally {
      setMergeSubmitting(false);
    }
  }

  /** 点击「提交制作范围」：展开项目级制作范围表单（复选框默认全选当前
   * 已确认片段；「全部已确认片段」开关默认开启）。 */
  function handleScopeOpen(): void {
    const items = state.kind === "ready" ? state.items : [];
    const selected: Record<string, boolean> = {};
    for (const f of items) {
      if (f.state === "confirmed") {
        selected[f.object_ref.id] = true;
      }
    }
    setScoping({ open: true, all: true, selected, submitting: false });
  }

  /** 点击「取消」：回到按钮态（零 API 调用）。 */
  function handleScopeCancel(): void {
    setScoping({ open: false, all: true, selected: {}, submitting: false });
  }

  /** 切换「全部已确认片段」开关（开启 = 提交时省略 fragment_ids）。 */
  function handleScopeAllToggle(checked: boolean): void {
    setScoping((prev) => ({ ...prev, all: checked }));
  }

  /** 切换单个已确认片段的勾选。 */
  function handleScopeToggle(fid: string, checked: boolean): void {
    setScoping((prev) => ({
      ...prev,
      selected: { ...prev.selected, [fid]: checked },
    }));
  }

  /** 点击「提交」：本地校验（关闭「全部」开关且勾选为零 → 本地提示，不发
   * 请求、保留表单）→ 每次点击生成一次 commandId → createProductionScope
   * （all 时省略 fragment_ids）。 */
  async function handleScopeSubmit(): Promise<void> {
    const selectedIds = Object.keys(scoping.selected).filter(
      (id) => scoping.selected[id],
    );
    if (!scoping.all && selectedIds.length === 0) {
      // 本地拦截：零勾选不发请求，保留表单（文案与服务端 rule=required 一致）
      setScoping((prev) => ({
        ...prev,
        error: {
          ok: false,
          code: "validation_failed",
          message: "请至少选择一个已确认片段。",
          details: {
            violations: [
              {
                field: "fragment_ids",
                rule: "required",
                message: "请至少选择一个已确认片段。",
              },
            ],
          },
        },
      }));
      return;
    }
    setScoping((prev) => ({ ...prev, submitting: true }));
    try {
      // 每次点击生成一次 commandId（显式点击，无自动提交）
      const res = await createProductionScope(clientObj, projectId, {
        fragmentIds: scoping.all ? undefined : selectedIds,
        commandId: makeCommandId(),
      });
      if (res.ok) {
        // 成功（201 首发与 200 幂等重放同等处理）：清除行内错误与表单 +
        // 内部重取（片段 + 当前范围）
        setScoping({ open: false, all: true, selected: {}, submitting: false });
        setRefreshTick((t) => t + 1);
      } else {
        // 失败：行内错误（保留表单与勾选，不退出）
        setScoping((prev) => ({ ...prev, error: res }));
      }
    } finally {
      setScoping((prev) => ({ ...prev, submitting: false }));
    }
  }

  /** 制作范围行内错误文案：duplicate_scope → 固定文案；precondition_failed
   * → error.message；validation_failed → violations 每条 message；其他 →
   * error.message。 */
  function scopeErrorText(error: ScopeError): string {
    if (error.code === "duplicate_scope") {
      return "相同片段覆盖的制作范围已提交过，历史只追加不重复。";
    }
    if (error.code === "precondition_failed") {
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
    return error.message;
  }

  /**
   * 行内错误文案（确认/改名/退役/边界/拆分/合并统一）：validation_failed →
   * violations 每条 message；range_invalid → error.message（含 canonical
   * 长度、当前 range 与 split_point）；range_overlap → 列出 conflicts
   * 每个冲突（name + 范围）；preview_stale → 「预览已过期，请重新预览。」；
   * revision_conflict → 确认/改名「片段版本已变化，请刷新后重试。」、
   * 退役/边界/拆分/合并「版本已变化，请刷新后重试。」；precondition_failed
   * → not_contiguous 显示 error.message（含两片范围数值定位）、其余
   * 「片段状态已变化，请刷新后重试。」；其他 message。
   */
  function rowErrorText(error: RowError, source?: RowErrorSource): string {
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
    if (error.code === "range_invalid") {
      // 显示 error.message（含 canonical 长度，如「范围为空：end 必须大于
      // start（正文长度 N 个 UTF-16 单位）。」）
      return error.message;
    }
    if (error.code === "range_overlap") {
      // 列出 details.conflicts 每个冲突（name + 范围 start/end），冲突可定位
      const details = error.details as Record<string, unknown> | undefined;
      const conflicts = (details?.conflicts ?? []) as Array<{
        name?: string;
        start?: number;
        end?: number;
      }>;
      const list = conflicts
        .map((c) => `${c.name ?? ""}（${c.start ?? "?"}–${c.end ?? "?"}）`)
        .join("、");
      return `该范围与已有片段重叠（不自动裁切）：${list}`;
    }
    if (error.code === "preview_stale") {
      return "预览已过期，请重新预览。";
    }
    if (error.code === "revision_conflict") {
      return source === "retire" ||
        source === "boundary" ||
        source === "split" ||
        source === "merge"
        ? "版本已变化，请刷新后重试。"
        : "片段版本已变化，请刷新后重试。";
    }
    if (error.code === "precondition_failed") {
      // not_contiguous → 显示 error.message（含两片范围数值定位，按请求序
      // 报告）；其余 reason（source_revision_inactive/already_retired）→
      // 「片段状态已变化，请刷新后重试。」
      const details = error.details as Record<string, unknown> | undefined;
      if (details?.reason === "not_contiguous") return error.message;
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
    // 项目级合并动作区（列表上方，非行级）：选项 = 当前列表中
    // state === "candidate" 或 state === "confirmed" 的片段
    const mergeableFragments = state.items.filter(
      (f) => f.state === "candidate" || f.state === "confirmed",
    );
    // 项目级制作范围动作区（列表上方，与「合并」同层）：复选框选项仅为
    // 当前列表中 state === "confirmed" 的片段（候选/退役不出现）
    const scopableFragments = state.items.filter(
      (f) => f.state === "confirmed",
    );
    body = (
      <div className="fragment-actions__ready">
        <div className="fragment-actions__scope">
          {scopeError !== null ? (
            <div
              className="fragment-actions__scope-read-error"
              role="alert"
            >
              <p>制作范围读取失败：{scopeError.message}</p>
              <button
                type="button"
                className="fragment-actions__scope-retry"
                onClick={() => setRetryTick((t) => t + 1)}
              >
                重试
              </button>
            </div>
          ) : scope === null ? (
            <p className="fragment-actions__scope-empty">
              尚未提交制作范围。
            </p>
          ) : (
            <div className="fragment-actions__scope-current">
              <p className="fragment-actions__scope-count">
                覆盖片段清单（共 {scope.fragment_ids.length} 个片段）
              </p>
              {scope.covered ? (
                <p className="fragment-actions__scope-covered">
                  完整覆盖正文
                </p>
              ) : (
                <div className="fragment-actions__scope-excluded">
                  <p>范围外段落</p>
                  <ul className="fragment-actions__scope-excluded-list">
                    {scope.excluded_ranges.map((r) => (
                      <li key={`${r.start}-${r.end}`}>
                        [{r.start}, {r.end}) · 长度 {r.end - r.start}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <ul className="fragment-actions__scope-fragments">
                {scope.fragment_ids.map((id) => {
                  const named = state.items.find(
                    (it) => it.object_ref.id === id,
                  );
                  return <li key={id}>{named !== undefined ? named.name : id}</li>;
                })}
              </ul>
            </div>
          )}
          {scoping.open ? (
            <div className="fragment-actions__scope-form">
              <label className="fragment-actions__scope-all">
                <input
                  type="checkbox"
                  className="fragment-actions__scope-all-input"
                  checked={scoping.all}
                  onChange={(e) => handleScopeAllToggle(e.target.checked)}
                />
                全部已确认片段
              </label>
              {!scoping.all && (
                <ul className="fragment-actions__scope-choices">
                  {scopableFragments.map((f) => (
                    <li key={f.object_ref.id}>
                      <label className="fragment-actions__scope-choice">
                        <input
                          type="checkbox"
                          className="fragment-actions__scope-choice-input"
                          checked={scoping.selected[f.object_ref.id] === true}
                          onChange={(e) =>
                            handleScopeToggle(
                              f.object_ref.id,
                              e.target.checked,
                            )
                          }
                        />
                        {f.name}
                      </label>
                    </li>
                  ))}
                </ul>
              )}
              <button
                type="button"
                className="fragment-actions__scope-submit"
                disabled={scoping.submitting}
                onClick={() => void handleScopeSubmit()}
              >
                提交
              </button>
              <button
                type="button"
                className="fragment-actions__scope-cancel"
                onClick={() => handleScopeCancel()}
              >
                取消
              </button>
              {scoping.error !== undefined && (
                <div
                  className="fragment-actions__scope-error"
                  role="alert"
                >
                  <p>{scopeErrorText(scoping.error)}</p>
                </div>
              )}
            </div>
          ) : (
            <button
              type="button"
              className="fragment-actions__scope-open"
              onClick={() => handleScopeOpen()}
            >
              提交制作范围
            </button>
          )}
        </div>
        {merging.open ? (
          <div className="fragment-actions__merge-form">
            {merging.stage === "confirm" &&
              merging.preview !== undefined && (
                <p className="fragment-actions__merge-impact">
                  影响：{merging.preview.impact.affected.length} 项 /
                  待复核：{merging.preview.impact.needs_review.length}{" "}
                  项 / 可保留：
                  {merging.preview.impact.preservable.length} 项
                </p>
              )}
            {merging.stage === "preview" ? (
              <>
                <select
                  className="fragment-actions__merge-select-a"
                  value={merging.fragmentAId}
                  onChange={(e) =>
                    setMerging((prev) => ({
                      ...prev,
                      fragmentAId: e.target.value,
                    }))
                  }
                >
                  <option value="">选择片段 A</option>
                  {mergeableFragments.map((f) => (
                    <option key={f.object_ref.id} value={f.object_ref.id}>
                      {f.name} · {fragmentStateLabel(f.state)}
                    </option>
                  ))}
                </select>
                <select
                  className="fragment-actions__merge-select-b"
                  value={merging.fragmentBId}
                  onChange={(e) =>
                    setMerging((prev) => ({
                      ...prev,
                      fragmentBId: e.target.value,
                    }))
                  }
                >
                  <option value="">选择片段 B</option>
                  {mergeableFragments.map((f) => (
                    <option key={f.object_ref.id} value={f.object_ref.id}>
                      {f.name} · {fragmentStateLabel(f.state)}
                    </option>
                  ))}
                </select>
                <input
                  className="fragment-actions__merge-name"
                  value={merging.mergedName}
                  onChange={(e) =>
                    setMerging((prev) => ({
                      ...prev,
                      mergedName: e.target.value,
                    }))
                  }
                />
                <button
                  type="button"
                  className="fragment-actions__merge-preview"
                  disabled={mergeSubmitting}
                  onClick={() => void handleMergePreview()}
                >
                  预览
                </button>
              </>
            ) : (
              <button
                type="button"
                className="fragment-actions__merge-apply"
                disabled={mergeSubmitting}
                onClick={() => void handleMergeApply()}
              >
                确认合并
              </button>
            )}
            <button
              type="button"
              className="fragment-actions__merge-cancel"
              onClick={() => handleMergeCancel()}
            >
              取消
            </button>
            {mergeError !== null && (
              <div className="fragment-actions__merge-error" role="alert">
                <p>{rowErrorText(mergeError.error, mergeError.source)}</p>
              </div>
            )}
          </div>
        ) : (
          <button
            type="button"
            className="fragment-actions__merge"
            onClick={() => handleMergeOpen()}
          >
            合并
          </button>
        )}
        <ul className="fragment-actions__items">
        {state.items.map((fragment) => {
          const fid = fragment.object_ref.id;
          const errorEntry = errors[fid];
          const error = errorEntry?.error;
          const isSubmitting = submitting[fid] === true;
          const isCandidate = fragment.state === "candidate";
          // 改名仅 candidate/confirmed（retired 不显示；pending_review
          // 不会出现在 F2 列表中）
          const canRename =
            fragment.state === "candidate" || fragment.state === "confirmed";
          // 退役仅 candidate/confirmed（retired 不显示——F7 前置已 retired → 422；
          // F2 列表只列 active 版本绑定片段）
          const canRetire =
            fragment.state === "candidate" || fragment.state === "confirmed";
          // 边界仅 candidate/confirmed（retired 不显示——退役片段不再参与
          // 生产，无需调边界；与 14-b/c 同冻结）
          const canBoundary =
            fragment.state === "candidate" || fragment.state === "confirmed";
          // 拆分仅 candidate/confirmed（retired 不显示——F9 前置已 retired → 422；
          // 与 14-b/c/d 同冻结）
          const canSplit =
            fragment.state === "candidate" || fragment.state === "confirmed";
          const renameForm = renaming[fid];
          const isRenaming = renameForm?.open === true;
          const retireForm = retiring[fid];
          const isRetiring = retireForm?.open === true;
          const boundaryForm = boundary[fid];
          const isBoundary = boundaryForm?.open === true;
          const splitForm = splitting[fid];
          const isSplitting = splitForm?.open === true;
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
              ) : isRetiring ? (
                <div className="fragment-actions__retire-form">
                  {retireForm?.stage === "confirm" &&
                    retireForm?.preview !== undefined && (
                      <p className="fragment-actions__retire-impact">
                        影响：{retireForm.preview.impact.affected.length} 项 /
                        待复核：{retireForm.preview.impact.needs_review.length}{" "}
                        项 / 可保留：
                        {retireForm.preview.impact.preservable.length} 项
                      </p>
                    )}
                  {retireForm?.stage === "preview" ? (
                    <button
                      type="button"
                      className="fragment-actions__retire-preview"
                      disabled={isSubmitting}
                      onClick={() => void handleRetirePreview(fragment)}
                    >
                      预览影响
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="fragment-actions__retire-apply"
                      disabled={isSubmitting}
                      onClick={() => void handleRetireApply(fragment)}
                    >
                      确认退役
                    </button>
                  )}
                  <button
                    type="button"
                    className="fragment-actions__retire-cancel"
                    onClick={() => handleRetireCancel(fid)}
                  >
                    取消
                  </button>
                </div>
              ) : isBoundary ? (
                <div className="fragment-actions__boundary-form">
                  {boundaryForm?.stage === "confirm" &&
                    boundaryForm?.preview !== undefined && (
                      <p className="fragment-actions__boundary-impact">
                        影响：{boundaryForm.preview.impact.affected.length} 项 /
                        待复核：{boundaryForm.preview.impact.needs_review.length}{" "}
                        项 / 可保留：
                        {boundaryForm.preview.impact.preservable.length} 项
                      </p>
                    )}
                  {boundaryForm?.stage === "preview" ? (
                    <>
                      <input
                        type="number"
                        className="fragment-actions__boundary-start"
                        value={
                          boundaryForm?.start === null
                            ? ""
                            : String(boundaryForm.start)
                        }
                        onChange={(e) =>
                          updateBoundaryInput(fid, "start", e.target.value)
                        }
                      />
                      <input
                        type="number"
                        className="fragment-actions__boundary-end"
                        value={
                          boundaryForm?.end === null
                            ? ""
                            : String(boundaryForm.end)
                        }
                        onChange={(e) =>
                          updateBoundaryInput(fid, "end", e.target.value)
                        }
                      />
                      <button
                        type="button"
                        className="fragment-actions__boundary-preview"
                        disabled={isSubmitting}
                        onClick={() => void handleBoundaryPreview(fragment)}
                      >
                        预览
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="fragment-actions__boundary-apply"
                      disabled={isSubmitting}
                      onClick={() => void handleBoundaryApply(fragment)}
                    >
                      确认调整
                    </button>
                  )}
                  <button
                    type="button"
                    className="fragment-actions__boundary-cancel"
                    onClick={() => handleBoundaryCancel(fid)}
                  >
                    取消
                  </button>
                </div>
              ) : isSplitting ? (
                <div className="fragment-actions__split-form">
                  {splitForm?.stage === "confirm" &&
                    splitForm?.preview !== undefined && (
                      <p className="fragment-actions__split-impact">
                        影响：{splitForm.preview.impact.affected.length} 项 /
                        待复核：{splitForm.preview.impact.needs_review.length}{" "}
                        项 / 可保留：
                        {splitForm.preview.impact.preservable.length} 项
                      </p>
                    )}
                  {splitForm?.stage === "preview" ? (
                    <>
                      <input
                        type="number"
                        className="fragment-actions__split-point"
                        value={
                          splitForm?.splitPoint === null
                            ? ""
                            : String(splitForm.splitPoint)
                        }
                        onChange={(e) =>
                          updateSplitInput(fid, "splitPoint", e.target.value)
                        }
                      />
                      <input
                        className="fragment-actions__split-left"
                        value={splitForm?.leftName ?? ""}
                        onChange={(e) =>
                          updateSplitInput(fid, "leftName", e.target.value)
                        }
                      />
                      <input
                        className="fragment-actions__split-right"
                        value={splitForm?.rightName ?? ""}
                        onChange={(e) =>
                          updateSplitInput(fid, "rightName", e.target.value)
                        }
                      />
                      <button
                        type="button"
                        className="fragment-actions__split-preview"
                        disabled={isSubmitting}
                        onClick={() => void handleSplitPreview(fragment)}
                      >
                        预览
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="fragment-actions__split-apply"
                      disabled={isSubmitting}
                      onClick={() => void handleSplitApply(fragment)}
                    >
                      确认拆分
                    </button>
                  )}
                  <button
                    type="button"
                    className="fragment-actions__split-cancel"
                    onClick={() => handleSplitCancel(fid)}
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
                  {canRetire && (
                    <button
                      type="button"
                      className="fragment-actions__retire"
                      onClick={() => handleRetireOpen(fragment)}
                    >
                      退役
                    </button>
                  )}
                  {canBoundary && (
                    <button
                      type="button"
                      className="fragment-actions__boundary"
                      onClick={() => handleBoundaryOpen(fragment)}
                    >
                      边界
                    </button>
                  )}
                  {canSplit && (
                    <button
                      type="button"
                      className="fragment-actions__split"
                      onClick={() => handleSplitOpen(fragment)}
                    >
                      拆分
                    </button>
                  )}
                </>
              )}
              {error !== undefined && (
                <div className="fragment-actions__row-error" role="alert">
                  <p>{rowErrorText(error, errorEntry?.source)}</p>
                </div>
              )}
            </li>
          );
        })}
        </ul>
      </div>
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
        .fragment-actions__retire {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #e8590c;
          border-radius: 4px;
          background: #fff;
          color: #e8590c;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__retire-form {
          flex-basis: 100%;
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
          margin-top: 0.25rem;
        }
        .fragment-actions__retire-impact {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #495057;
          line-height: 1.5;
        }
        .fragment-actions__retire-preview {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #1971c2;
          border-radius: 4px;
          background: #fff;
          color: #1971c2;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__retire-preview:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__retire-apply {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #e8590c;
          border-radius: 4px;
          background: #e8590c;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__retire-apply:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__retire-cancel {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__boundary {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #5f3dc4;
          border-radius: 4px;
          background: #fff;
          color: #5f3dc4;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__boundary-form {
          flex-basis: 100%;
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
          margin-top: 0.25rem;
        }
        .fragment-actions__boundary-start,
        .fragment-actions__boundary-end {
          width: 5rem;
          padding: 0.3rem 0.5rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.85rem;
        }
        .fragment-actions__boundary-impact {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #495057;
          line-height: 1.5;
        }
        .fragment-actions__boundary-preview {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #1971c2;
          border-radius: 4px;
          background: #fff;
          color: #1971c2;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__boundary-preview:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__boundary-apply {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #5f3dc4;
          border-radius: 4px;
          background: #5f3dc4;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__boundary-apply:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__boundary-cancel {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__split {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #d9480f;
          border-radius: 4px;
          background: #fff;
          color: #d9480f;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__split-form {
          flex-basis: 100%;
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
          margin-top: 0.25rem;
        }
        .fragment-actions__split-point {
          width: 5rem;
          padding: 0.3rem 0.5rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.85rem;
        }
        .fragment-actions__split-left,
        .fragment-actions__split-right {
          width: 7rem;
          padding: 0.3rem 0.5rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.85rem;
        }
        .fragment-actions__split-impact {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #495057;
          line-height: 1.5;
        }
        .fragment-actions__split-preview {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #1971c2;
          border-radius: 4px;
          background: #fff;
          color: #1971c2;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__split-preview:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__split-apply {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #d9480f;
          border-radius: 4px;
          background: #d9480f;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__split-apply:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__split-cancel {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__ready {
          display: block;
        }
        .fragment-actions__merge {
          padding: 0.2rem 0.7rem;
          border: 1px solid #6741bc;
          border-radius: 4px;
          background: #fff;
          color: #6741bc;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__merge-form {
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
        .fragment-actions__merge-select-a,
        .fragment-actions__merge-select-b {
          padding: 0.3rem 0.5rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.85rem;
        }
        .fragment-actions__merge-name {
          flex: 1;
          min-width: 8rem;
          padding: 0.3rem 0.5rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          font-size: 0.85rem;
        }
        .fragment-actions__merge-impact {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #495057;
          line-height: 1.5;
        }
        .fragment-actions__merge-preview {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #1971c2;
          border-radius: 4px;
          background: #fff;
          color: #1971c2;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__merge-preview:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__merge-apply {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #6741bc;
          border-radius: 4px;
          background: #6741bc;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__merge-apply:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__merge-cancel {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__merge-error {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #fa5252;
          line-height: 1.5;
        }
        .fragment-actions__row-error {
          flex-basis: 100%;
          margin: 0.25rem 0 0;
          font-size: 0.85rem;
          color: #fa5252;
          line-height: 1.5;
        }
        .fragment-actions__scope {
          margin: 0 0 0.5rem;
          padding: 0.5rem 0;
          border-bottom: 1px solid #e9ecef;
          display: flex;
          flex-direction: column;
          align-items: flex-start;
          gap: 0.5rem;
        }
        .fragment-actions__scope-empty {
          margin: 0;
          font-size: 0.85rem;
          color: #868e96;
        }
        .fragment-actions__scope-current {
          display: flex;
          flex-direction: column;
          gap: 0.25rem;
          font-size: 0.85rem;
          color: #495057;
        }
        .fragment-actions__scope-current p {
          margin: 0;
        }
        .fragment-actions__scope-covered {
          color: #1b4d24;
        }
        .fragment-actions__scope-excluded-list,
        .fragment-actions__scope-fragments,
        .fragment-actions__scope-choices {
          list-style: none;
          margin: 0;
          padding: 0 0 0 1rem;
        }
        .fragment-actions__scope-form {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
        }
        .fragment-actions__scope-all,
        .fragment-actions__scope-choice {
          display: inline-flex;
          align-items: center;
          gap: 0.3rem;
          font-size: 0.85rem;
          color: #1a1a1a;
        }
        .fragment-actions__scope-choices {
          flex-basis: 100%;
          display: flex;
          flex-direction: column;
          gap: 0.25rem;
        }
        .fragment-actions__scope-open {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #7048e8;
          border-radius: 4px;
          background: #fff;
          color: #7048e8;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__scope-submit {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #7048e8;
          border-radius: 4px;
          background: #7048e8;
          color: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__scope-submit:disabled {
          cursor: not-allowed;
          opacity: 0.45;
        }
        .fragment-actions__scope-cancel {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
        }
        .fragment-actions__scope-error {
          flex-basis: 100%;
          margin: 0;
          font-size: 0.85rem;
          color: #fa5252;
          line-height: 1.5;
        }
        .fragment-actions__scope-read-error {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 0.5rem;
          font-size: 0.85rem;
          color: #fa5252;
        }
        .fragment-actions__scope-read-error p {
          margin: 0;
        }
        .fragment-actions__scope-retry {
          flex-shrink: 0;
          padding: 0.2rem 0.7rem;
          border: 1px solid #adb5bd;
          border-radius: 4px;
          background: #fff;
          cursor: pointer;
          font-size: 0.85rem;
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
