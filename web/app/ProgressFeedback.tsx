'use client';

import StreamText from './StreamText';
import {Check, CircleAlert, LoaderCircle, Sparkles} from 'lucide-react';
import './progress-feedback.css';
import './author/production-progress.css';
import StyleProgress from './StyleProgress';
import type {StyleLive} from './StyleProgress';
import TaskActivity from './TaskActivity';
import type {ActivityDisplay} from './TaskActivity';
import type {GenerationInfo} from './GenerationPrompt';
import type {SkillCall} from './NodeSkills';
import ProviderError from './ProviderError';
import type {ProviderFailure} from './ProviderError';

export type ProgressTask = {production_phase?: string | null; production_node?: string | null; id: string; kind: string; status: string; progress?: number; message?: string; created?: number; work_id?: string | null; label?: string | null; preview?: string | null; revision_of?: string | null; activity?: ActivityDisplay | null; generation?: GenerationInfo | null; provider_error?:ProviderFailure|null; skill_calls?:SkillCall[]; result?: {live?: StyleLive; replaced_assets?: string[]; media?: string}};
export const activeStatuses = ['queued', 'running', 'waiting'];
export const problemStatuses = ['failed', 'needs_review'];
export const taskNames: Record<string, string> = {author_styles: '推荐创作风格', author_flow: '推进制作流程', author_storyboard: '分镜生成', author_render: '漫剧生成', reader_branch_plan: '规划读者分支', director: '剧本与分镜', art_design: '人物与场景设计', image: '生成画面', video: '生成视频', creative_watch: '编排镜头', creative_revision: '按意见修改', plan: '理解剧情想法', render: '制作过渡片段', bridge: '衔接故事', export: '导出作品'};
export const statusNames: Record<string, string> = {queued: '排队中', running: '处理中', waiting: '等待结果', completed: '已完成', failed: '处理失败', needs_review: '需要处理', cancelled: '已停止', superseded: '已有新版本'};
export const workStages = [
  {id: 'style', name: '选择风格', hint: '阅读微小说，确定画风与剧情气质'},
  {id: 'segments_review', name: '确认情节', hint: '把原文切成有序情节，逐段核对后开始制作'},
  {id: 'preparing', name: '准备形象', hint: '只为情节里出场的人物与场景生成参考图'},
  {id: 'assets_review', name: '确认图片', hint: '查看形象，提出修改或确认继续'},
  {id: 'storyboarding', name: '分镜生成', hint: '按情节逐个规划镜头、编写提示词并完成文本预审'},
  {id: 'rendering', name: '漫剧生成', hint: '用已审核的项目参考图逐个情节生成视频'},
  {id: 'episode_review', name: '本集发布', hint: '每完成一集就决定：发布给读者，还是继续下一集'},
  {id: 'film_review', name: '审片验收', hint: '观看全部片段，确认最终效果'},
  {id: 'published', name: '发布作品', hint: '进入读者空间，供读者观看'},
];

// Kept for existing callers; real streaming views provide their own stable stream key.
export function Typewriter({text}: {text: string}) {
  return <StreamText text={text} active streamKey="static-typewriter"/>;
}

export function ProgressBar({value, label}: {value?: number; label: string}) {
  const known = value !== undefined && Number.isFinite(value);
  const percent = known ? Math.max(0, Math.min(100, value!)) : undefined;
  return <div className={'feedback-bar' + (known ? '' : ' is-indeterminate')} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><i style={known ? {width: `${percent}%`} : undefined} /></div>;
}

export function TaskProgress({task, detailed = false}: {task: ProgressTask; detailed?: boolean}) {
  if (task.kind === 'author_styles') return <StyleProgress task={task}/>;
  if (task.activity) return <TaskActivity task={task} detailed={detailed}/>;
  if (detailed && ['director','art_design','creative_revision','plan','image','video'].includes(task.kind)) {
    const media = task.kind === 'image' || task.kind === 'video';
    return <TaskActivity detailed task={{...task, activity: {type: media ? 'media' : 'model',
      title: task.label || taskNames[task.kind], phase: 'preparing', message: task.message || '等待开始处理',
      started_at: task.created || 0, updated_at: task.created || 0, summary: '', items: [], history: [], events: []}}}/>;
  }
  const active = activeStatuses.includes(task.status);
  const problem = problemStatuses.includes(task.status);
  const done = task.status === 'completed';
  const value = done ? 100 : !active || task.status === 'queued' ? 0 : (task.progress || undefined);
  return <div className={'task-progress' + (problem ? ' has-problem' : '')}>
    <div className="feedback-title"><span>{active ? <LoaderCircle size={15} className="feedback-spin" /> : problem ? <CircleAlert size={15} /> : done ? <Check size={15} /> : <Sparkles size={15} />}{taskNames[task.kind] || '制作任务'}</span><small>{statusNames[task.status] || task.status}</small></div>
    {task.message && <p>{task.message}</p>}
    <ProviderError error={task.provider_error}/>
    <ProgressBar value={value} label={(taskNames[task.kind] || '任务') + '阶段进度'} />
  </div>;
}

export function InteractionFeedback({busy, text, title = '创作助手', error = false}: {busy?: boolean; text: string; title?: string; error?: boolean}) {
  if (!busy && !text) return null;
  return <div className={'interaction-feedback' + (error ? ' has-problem' : '')} role={error ? 'alert' : 'status'} aria-live="polite" aria-atomic="true" aria-busy={busy || undefined}>
    <div className="feedback-title"><span>{busy ? <LoaderCircle size={16} className="feedback-spin" /> : error ? <CircleAlert size={16} /> : <Sparkles size={16} />}{title}</span><small>{busy ? '进行中' : error ? '需要处理' : '已回复'}</small></div>
    <p>{text}</p>
  </div>;
}

export function WorkProgress({stage, viewedStage, onStepSelect, disabled = false, compact = false}: {stage?: string; viewedStage?: string; onStepSelect?: (stage:string)=>void; disabled?:boolean; compact?: boolean}) {
  const current = workStages.findIndex(s => s.id === stage);
  const viewed = workStages.findIndex(s => s.id === (viewedStage || stage));
  // The column count follows the step list, so adding a step never wraps the rail onto a second row.
  return <ol className={'work-progress' + (compact ? ' is-compact' : '')} aria-label="作品制作阶段"
             style={{'--stage-count': workStages.length} as React.CSSProperties}>
    {workStages.map((s, index) => {
      const content=<><span className="stage-dot">{index < current || stage === 'published' ? <Check size={16} /> : String(index + 1).padStart(2, '0')}</span><div><strong>{s.name}</strong>{!compact && <small>{s.hint}</small>}</div></>;
      return <li key={s.id} className={index === viewed ? 'is-current' : index < current || stage === 'published' ? 'is-done' : ''} aria-current={index === viewed ? 'step' : undefined}>
        {onStepSelect ? <button type="button" className="stage-link" disabled={disabled || index > current || current < 0} aria-label={(index < current ? '返回' : '查看')+s.name} onClick={()=>onStepSelect(s.id)}>{content}</button> : content}
      </li>;
    })}
  </ol>;
}
