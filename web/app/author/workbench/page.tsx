'use client';
import { useEffect, useReducer, useState } from 'react';
import WorkbenchShell from './WorkbenchShell';
import { createWorkbenchState, workbenchReducer } from './workspace-state';
import './workbench.css';

async function readProject(id: string, signal: AbortSignal): Promise<{ title?: string; author?: string; stage?: string } | null> {
  const r = await fetch('/api/author/projects/' + encodeURIComponent(id), { signal });
  if (!r.ok) return null;
  const d: any = await r.json();
  return {
    title: typeof d?.title === 'string' ? d.title : undefined,
    author: d?.source && typeof d.source.author_name === 'string' ? d.source.author_name : undefined,
    stage: typeof d?.stage === 'string' ? d.stage : undefined,
  };
}

// 新工作台入口 /author/workbench?work=<id>：以「项目」为核心，与现有 /author 生产流程并存、互不干扰。
// 本期（F1）只读现有项目、只搭框架外壳；不做任何业务操作。
export default function AuthorWorkbench() {
  const [state, dispatch] = useReducer(workbenchReducer, '', createWorkbenchState);
  const [hasWork, setHasWork] = useState(false);
  const [title, setTitle] = useState<string | null>(null);
  const [author, setAuthor] = useState<string | null>(null);
  const [stage, setStage] = useState<string | null>(null);
  const [readError, setReadError] = useState<string | null>(null);

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('work');
    if (!id) return;
    setHasWork(true);
    dispatch({ type: 'SET_PROJECT_ID', id });
    const controller = new AbortController();
    readProject(id, controller.signal)
      .then((d) => {
        if (d) {
          setTitle(d.title ?? null);
          setAuthor(d.author ?? null);
          setStage(d.stage ?? null);
          setReadError(null);
        } else {
          setReadError('项目不存在或无法读取');
        }
      })
      .catch((e) => {
        if ((e as Error)?.name !== 'AbortError') setReadError('读取项目失败，请稍后再试');
      });
    return () => controller.abort();
  }, []);

  return (
    <WorkbenchShell
      view={state}
      dispatch={dispatch}
      title={title}
      author={author}
      stage={stage}
      readError={readError}
      hasWork={hasWork}
    />
  );
}
