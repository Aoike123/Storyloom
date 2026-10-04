"use client";
import { X, Check, Loader2, Circle } from "lucide-react";

// 项目流水线步骤（与顶栏进度同源）
const PIPELINE = [
  "style",
  "segments_review",
  "preparing",
  "assets_review",
  "storyboarding",
  "rendering",
  "episode_review",
  "film_review",
  "published",
];
const LABELS: Record<string, string> = {
  style: "选择风格",
  segments_review: "确认情节",
  preparing: "准备形象",
  assets_review: "确认图片",
  storyboarding: "分镜生成",
  rendering: "漫剧生成",
  episode_review: "本集发布",
  film_review: "审片验收",
  published: "发布作品",
};

type Status = "done" | "current" | "todo";

interface Props {
  currentStage: string | null;
  onClose: () => void;
}

// 项目任务队列弹层：按流水线顺序列出各步骤状态（由项目当前 stage 派生：之前=已完成 / 当前=进行中 / 之后=待处理）。
// 只读进度视图；真实任务写入属服务端（D07 待定）。
export default function TaskQueue({ currentStage, onClose }: Props) {
  const idx = currentStage ? PIPELINE.indexOf(currentStage) : -1;
  return (
    <div
      className="workbench-taskqueue"
      role="dialog"
      aria-label="项目任务队列"
    >
      <div className="workbench-taskqueue-head">
        <span>项目任务队列</span>
        <button
          type="button"
          className="button icon"
          aria-label="关闭任务队列"
          onClick={onClose}
        >
          <X size={15} />
        </button>
      </div>
      <ul className="workbench-taskqueue-list">
        {PIPELINE.map((s, i) => {
          const status: Status =
            idx < 0
              ? "todo"
              : i < idx
                ? "done"
                : i === idx
                  ? "current"
                  : "todo";
          const Icon =
            status === "done" ? Check : status === "current" ? Loader2 : Circle;
          return (
            <li key={s} className={"workbench-taskqueue-item is-" + status}>
              <Icon
                size={15}
                className={status === "current" ? "is-spin" : undefined}
              />
              <span className="workbench-taskqueue-label">{LABELS[s]}</span>
              <small className="workbench-taskqueue-status">
                {status === "done"
                  ? "已完成"
                  : status === "current"
                    ? "进行中"
                    : "待处理"}
              </small>
            </li>
          );
        })}
      </ul>
      <small className="workbench-taskqueue-foot">
        状态由项目阶段派生 · 任务写入待服务端（D07）
      </small>
    </div>
  );
}
