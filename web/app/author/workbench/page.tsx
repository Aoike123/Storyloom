"use client";
import { useCallback, useEffect, useReducer, useMemo, useState } from "react";
import WorkbenchShell from "./WorkbenchShell";
import { createWorkbenchState, workbenchReducer } from "./workspace-state";
import { mapProject, type WorkInput } from "./project-adapter";
import {
  buildSourceModel,
  confirmFragment,
  createFragment,
  type SourceModel,
} from "./source-model";
import { authHeaders } from "../../auth";
import "./workbench.css";

// 新工作台入口 /author/workbench?work=<id>：以「项目」为核心，与现有 /author 生产流程并存、互不干扰。
// 视图状态（前端）与业务状态（服务端）分离。本组件负责：读项目、真实导入原文（POST /projects/{id}/story）、
// 空项目落「切分」、并按“是否本会话刚导入”决定是否预置示例片段。
// 读请求依赖登录态（sl_auth cookie 兜底，见 auth.ts）；导入（owner-scoped 写）显式带 Authorization 头。
export default function AuthorWorkbench() {
  const [state, dispatch] = useReducer(
    workbenchReducer,
    "",
    createWorkbenchState,
  );
  const [hasWork, setHasWork] = useState(false);
  const [work, setWork] = useState<WorkInput | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [importedInSession, setImportedInSession] = useState(false);

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("work");
    if (!id) return;
    setHasWork(true);
    dispatch({ type: "SET_PROJECT_ID", id });
    const controller = new AbortController();
    fetch("/api/author/projects/" + encodeURIComponent(id), {
      signal: controller.signal,
    })
      .then((r) => {
        if (controller.signal.aborted) return null;
        if (r.status === 401) {
          setReadError("登录后可打开你的项目");
          return null;
        }
        if (!r.ok) {
          setReadError("项目不存在或无法读取");
          return null;
        }
        return r.json();
      })
      .then((d: WorkInput | null) => {
        if (d === null) return;
        if (!d || typeof d !== "object") {
          setReadError("项目不存在或无法读取");
          return;
        }
        setWork(d);
        setReadError(null);
        // 空项目（尚无原文）→ 起始标签落「切分」；已有原文的项目保持「分镜」默认
        if (!d.source)
          dispatch({ type: "SET_INITIAL_WORKSPACE", workspace: "cut" });
      })
      .catch((e: unknown) => {
        if ((e as Error)?.name !== "AbortError")
          setReadError("项目不存在或无法读取");
      });
    return () => controller.abort();
  }, []);

  const content = useMemo(() => (work ? mapProject(work) : null), [work]);

  // 生效的来源模型：沿用项目原文与真实片段（D03）；本会话刚导入自己的故事时走干净空白（不预置示例片段）。
  const source = useMemo<SourceModel | null>(() => {
    if (!work) return null;
    const text = work.source?.content ?? "";
    const revision =
      work.source_revision ||
      work.source?.content_hash ||
      (work.source?.completeness === "full" ? "import-1" : "import-0");
    return buildSourceModel(text, revision, {
      serverFragments: work.fragments ?? [],
      withExamples: !importedInSession,
    });
  }, [work, importedInSession]);

  // 重新拉取项目（片段写入后刷新来源模型与真实片段）。
  const refreshWork = useCallback(async () => {
    const pid = state.projectId;
    if (!pid) return;
    const r = await fetch("/api/author/projects/" + encodeURIComponent(pid));
    if (!r.ok) return;
    const d = await r.json().catch(() => null);
    if (d && typeof d === "object") setWork(d as WorkInput);
  }, [state.projectId]);

  // D03：真实建立候选片段（服务端校验范围合法、非空、不重叠、版本匹配），成功后刷新。
  async function addCandidate(range: { start: number; end: number }) {
    const pid = state.projectId;
    if (!pid || !source) return;
    try {
      await createFragment(pid, range, source.revision);
      await refreshWork();
    } catch (e) {
      throw e;
    }
  }

  // D03：确认候选片段（独立操作），成功后刷新。
  async function confirmFragmentById(fragmentId: string) {
    const pid = state.projectId;
    if (!pid) return;
    try {
      await confirmFragment(pid, fragmentId);
      await refreshWork();
    } catch (e) {
      throw e;
    }
  }

  // 真实导入原文：POST /projects/{pid}/story（owner-scoped，带 Authorization 头）。
  // 成功返回刷新后的 workspace（含 source）；导入后落「切分」并停用示例片段预置。
  async function importStory(text: string, title: string) {
    const pid = state.projectId;
    if (!pid) return;
    setImportBusy(true);
    setImportError(null);
    try {
      const r = await fetch(
        "/api/author/projects/" + encodeURIComponent(pid) + "/story",
        {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({ content: text, title: title || null }),
        },
      );
      const d = await r.json().catch(() => ({}));
      if (!r.ok)
        throw new Error(
          typeof d.detail === "string" ? d.detail : "导入失败，请重试。",
        );
      setWork(d as WorkInput);
      setImportedInSession(true);
      setReadError(null);
      dispatch({ type: "SET_INITIAL_WORKSPACE", workspace: "cut" });
    } catch (e) {
      setImportError(e instanceof Error ? e.message : "导入失败，请重试。");
    } finally {
      setImportBusy(false);
    }
  }

  return (
    <WorkbenchShell
      view={state}
      dispatch={dispatch}
      title={work?.title ?? null}
      author={work?.source?.author_name ?? null}
      stage={work?.stage ?? null}
      readError={readError}
      hasWork={hasWork}
      source={source}
      content={content?.byWorkspace ?? null}
      onImportStory={importStory}
      importBusy={importBusy}
      importError={importError}
      onAddCandidate={addCandidate}
      onConfirmFragment={confirmFragmentById}
    />
  );
}
