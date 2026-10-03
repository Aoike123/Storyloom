'use client';
import { useEffect, useReducer, useMemo, useState } from 'react';
import WorkbenchShell from './WorkbenchShell';
import { createWorkbenchState, workbenchReducer } from './workspace-state';
import { mapProject, type WorkInput } from './project-adapter';
import './workbench.css';

// 新工作台入口 /author/workbench?work=<id>：以「项目」为核心，与现有 /author 生产流程并存、互不干扰。
// 只读现有项目：读 work → project-adapter 映射为四个工作区的展示模型 → 装配 Shell。
// 视图状态（前端）与业务状态（服务端）严格分离；本组件只把 work 交给 adapter，不在组件内读业务字段做流程判断。
export default function AuthorWorkbench() {
  const [state, dispatch] = useReducer(workbenchReducer, '', createWorkbenchState);
  const [hasWork, setHasWork] = useState(false);
  const [work, setWork] = useState<WorkInput | null>(null);
  const [readError, setReadError] = useState<string | null>(null);

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('work');
    if (!id) return;
    setHasWork(true);
    dispatch({ type: 'SET_PROJECT_ID', id });
    const controller = new AbortController();
    fetch('/api/author/projects/' + encodeURIComponent(id), { signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then((d: WorkInput) => {
        if (!d || typeof d !== 'object') throw new Error('empty');
        setWork(d);
        setReadError(null);
      })
      .catch((e: unknown) => {
        if ((e as Error)?.name !== 'AbortError') setReadError('项目不存在或无法读取');
      });
    return () => controller.abort();
  }, []);

  const content = useMemo(() => (work ? mapProject(work) : null), [work]);

  return (
    <WorkbenchShell
      view={state}
      dispatch={dispatch}
      title={work?.title ?? null}
      author={work?.source?.author_name ?? null}
      stage={work?.stage ?? null}
      readError={readError}
      hasWork={hasWork}
      source={content?.source ?? null}
      content={content?.byWorkspace ?? null}
    />
  );
}
