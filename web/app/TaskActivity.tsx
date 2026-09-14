'use client';
import {useEffect, useState} from 'react';
import {Check, CircleAlert, LoaderCircle, Sparkles} from 'lucide-react';
import type {ProgressTask} from './ProgressFeedback';
import GenerationPrompt from './GenerationPrompt';
import {SkillCallTrace} from './NodeSkills';
import ProviderError from './ProviderError';
import StreamText, {StreamRegion} from './StreamText';

type Draft = {title: string; text: string};
export type ActivityDisplay = {
  type: 'model' | 'media'; title: string; phase: string; message: string;
  started_at: number; call_started_at?: number; updated_at: number;
  summary: string; items: Draft[];
  history: {title: string; summary: string; items: Draft[]}[];
  events: {phase: string; message: string; at: number}[];
};

export default function TaskActivity({task, detailed = false}: {task: ProgressTask; detailed?: boolean}) {
  const activity = task.activity!;
  const active = ['queued', 'running', 'waiting'].includes(task.status);
  const done = task.status === 'completed';
  const superseded = task.status === 'superseded';
  const cancelled = task.status === 'cancelled';
  const retired = superseded || cancelled;
  const retiredNote = superseded ? '此任务已因上游更新失效，记录保留供查看。' : '此任务已停止，已有记录保留供查看。';
  const problem = !active && !done && !retired;
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now() / 1000);
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [task.id, active]);
  const seconds = Math.max(0, Math.floor((active ? now : activity.updated_at) - activity.started_at));
  const elapsed = seconds < 60 ? seconds + ' 秒' : Math.floor(seconds / 60) + ' 分 ' + seconds % 60 + ' 秒';
  const model = activity.type === 'model';
  const outputImage = task.kind==='image' && done && task.result?.media?.startsWith('/media/') ? task.result.media : undefined;
  const displayImage = outputImage || task.preview;
  const imageCaption = outputImage ? '本次生成的参考图' : task.revision_of ? '修改前的参考图' : '本次参考画面';
  const mediaStep = done ? 3 : ['checking', 'saving', 'downloading'].includes(activity.phase) ? 2 : activity.phase === 'generating' ? 1 : 0;
  const emptyDraft = active ? '这一部分正在展开…' : retired ? retiredNote : problem ? '任务已暂停；这一项没有通过完整校验，请查看上方错误提示。' : '这一项未保存可展示内容。';
  const streamKey = task.id + ':' + (activity.call_started_at || activity.started_at);
  const drafts = <div className="activity-drafts" aria-label="逐步返回的创作内容">{activity.items?.map((item, index) => <article key={index}><strong>{item.title}</strong><p>{item.text ? <StreamText text={item.text} active={active && detailed && index === activity.items.length - 1} streamKey={streamKey + ':item:' + index} instant={problem || retired}/> : emptyDraft}</p></article>)}</div>;

  return <section className={'task-progress activity-progress' + (problem ? ' has-problem' : '')} aria-label={activity.title + '进展'}>
    <div className="feedback-title"><span>{active ? <LoaderCircle size={15} className="feedback-spin"/> : done ? <Check size={15}/> : retired ? <Sparkles size={15}/> : <CircleAlert size={15}/>}<strong>{activity.title}</strong></span><small>{active ? (task.status === 'queued' ? '已等待 ' : '已用时 ') + elapsed : done ? '已完成' : superseded ? '已失效 · 历史记录' : cancelled ? '已停止' : '需处理'}</small></div>
    <p className="activity-status" role="status">{task.message || activity.message}</p>
    <ProviderError error={task.provider_error}/>
    <SkillCallTrace calls={task.skill_calls}/>
    {detailed ? <>
      {model ? <>
        <div className="style-thinking"><div className="style-thinking-label"><Sparkles size={13}/><span>创作摘要</span><small>{active ? '持续更新' : done ? '已保留' : '保留的草稿'}</small></div>
          <StreamRegion active={active} streamKey={streamKey} className="style-thinking-body" tabIndex={activity.summary ? 0 : undefined} aria-label="模型生成的创作摘要"><p className={activity.summary ? '' : 'style-thinking-empty'}>{activity.summary ? <StreamText text={activity.summary} active={active} streamKey={streamKey + ':summary'} instant={problem || retired}/> : (retired ? retiredNote : problem ? '本次尚未收到可展示的创作摘要。' : done ? '本次任务未保存创作摘要，可查看已完成的结果。' : '收到的创作方向与检查重点会显示在这里。原文与已完成的素材可以继续查看。')}</p></StreamRegion>
        </div>
        <div className="activity-draft-heading"><span>{activity.items?.length ? '已返回 ' + activity.items.length + ' 项内容' : active ? '方案内容准备中' : '本轮没有可展示的方案内容'}</span><small>{active ? '生成中的草稿' : retired ? '历史记录' : done ? '本轮返回内容' : '任务已暂停，查看错误提示'}</small></div>
        {drafts}
      </> : <div className="activity-media-state">
        {displayImage && <figure><img src={displayImage} alt={imageCaption}/><figcaption>{imageCaption}</figcaption></figure>}
        <div><ol className="style-progress-steps activity-media-steps">{['准备输入', '模型生成', '检查保存'].map((label, index) => <li key={label} className={index < mediaStep ? 'is-done' : index === mediaStep && active ? 'is-current' : ''}><span>{index < mediaStep ? <Check size={12}/> : index + 1}</span>{label}</li>)}</ol>
          <p>{retired ? retiredNote : done ? '素材已保存，可以在下方预览。' : problem ? '已完成的素材会保留，可查看提示后处理。' : '画面和片段返回后会逐个出现在下方，可以先检查已完成的部分。'}</p>
          <small>{active ? '制作接口未提供逐字思考或准确剩余时间，当前展示实际处理状态。' : '最终使用前仍按当前流程确认画面和片段。'}</small>
        </div>
      </div>}
      <p className="activity-status-note">{active ? now - activity.updated_at > 25 ? '还在等待下一次返回；已收到的进展与素材会保留。' : model ? '内容在完整检查后才用于后续制作。' : '等待期间可以阅读原文、查看素材，也可以稍后回来。' : retired ? retiredNote : done ? '本轮记录已保留，后续步骤可继续查看。' : '部分内容保留为草稿，后续步骤不会使用未通过检查的结果。'}</p>
    </> : activity.summary ? <p className="activity-summary-brief">{activity.summary}</p> : null}
    {detailed && !model && <GenerationPrompt key={task.id} generation={task.generation} pending={active}/>}
    <details className="style-event-history activity-history"><summary>查看过程与内容 <span>{activity.events?.length || 0} 条记录</span></summary>
      {task.result?.media?.startsWith('/media/') && <a href={task.result.media} target="_blank" rel="noreferrer">查看本次保存的{task.kind === 'video' ? '片段' : '画面'} ↗</a>}
      {!detailed && !model && <GenerationPrompt key={task.id} generation={task.generation} pending={active}/>}
      {!detailed && !!activity.items?.length && drafts}
      {activity.history?.map((item, index) => <section className="activity-past-call" key={index}><strong>{item.title}</strong><p>{item.summary || '本轮内容已返回。'}</p>{item.items?.map((draft, i) => <p key={i}><b>{draft.title}</b> · {draft.text}</p>)}</section>)}
      <ol>{activity.events?.map((event, index) => <li key={index}><time>{Math.max(0, Math.floor(event.at - activity.started_at))} 秒</time><span>{event.message}</span></li>)}</ol>
    </details>
  </section>;
}
