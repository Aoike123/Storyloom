"use client";

import { useEffect, useReducer } from "react";

import {
  createInitialState,
  studioReducer,
} from "./shell/studio-state";

interface StudioAppProps {
  projectId: string | null;
  invalid?: boolean;
}

export default function StudioApp({ projectId, invalid }: StudioAppProps) {
  const [state, dispatch] = useReducer(studioReducer, undefined, createInitialState);

  // 项目切换：projectId prop 变化（含 invalid / null）→ 状态容器换项目，
  // selection/view/jobFeed 一并重置（项目隔离）。容器不做网络水合——
  // 项目 DTO 由后续卡经 hydrate 注入（本卡不新增 API 客户端）。
  useEffect(() => {
    dispatch({
      type: "setProject",
      projectId: invalid === true ? null : projectId,
    });
  }, [projectId, invalid]);

  let body: React.ReactNode;

  if (invalid === true) {
    body = <p className="studio-shell__msg">项目 ID 无效或为空，无法进入工作台。</p>;
  } else if (state.project.projectId === null) {
    body = <p className="studio-shell__msg">未选择项目。请从首页选择一个项目进入工作台。</p>;
  } else {
    body = (
      <p className="studio-shell__msg">
        项目 {state.project.projectId} 的工作台尚未建立内容（空态）。
      </p>
    );
  }

  return (
    <div className="studio-shell" data-view={state.view.current}>
      <style jsx>{`
        .studio-shell {
          min-height: 100vh;
          display: flex;
          align-items: center;
          justify-content: center;
          font-family:
            -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
            "Noto Sans", sans-serif;
          color: #1a1a1a;
          background: #f5f5f5;
          padding: 2rem;
        }
        .studio-shell__msg {
          font-size: 1.125rem;
          line-height: 1.6;
          color: #333;
          max-width: 32em;
          text-align: center;
        }
      `}</style>
      {body}
    </div>
  );
}
