'use client';

import {useEffect, useState} from 'react';
import {Check, CircleAlert, LoaderCircle, Sparkles} from 'lucide-react';
import type {ProgressTask} from './ProgressFeedback';
import StreamText, {StreamRegion} from './StreamText';

export type StyleLive = {
  phase: string;
  started_at: number;
  updated_at: number;
  summary: string;
  options: {art?: string; tone?: string; reason?: string; prompt?:string}[];
  events: {phase: string; message: string; at: number}[];
};

export default function StyleProgress({task, detailed = false, connection}: {task: ProgressTask; detailed?: boolean; connection?: string}) {
  const live = task.result?.live;
  const active = ['queued', 'running', 'waiting'].includes(task.status);
  const done = task.status === 'completed';
  const problem = ['failed', 'needs_review', 'cancelled', 'superseded'].includes(task.status);
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now() / 1000);
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [active, task.id]);
  const seconds = Math.max(0, Math.floor((active ? now : live?.updated_at || now) - (live?.started_at || task.created || now)));
  const clock = seconds < 60 ? seconds + ' 秒' : Math.floor(seconds / 60) + ' 分 ' + seconds % 60 + ' 秒';
  const phase = live?.phase;
  const step = done ? 4 : phase === 'ready' || phase === 'validating' || phase === 'buffered' ? 3 : live?.options?.length ? 2 : phase ? 1 : 0;
  const message = task.message === '等待执行' && task.status === 'running' ? '模型请求已开始，正在等待内容返回' : task.message;

  return <section className={'task-progress style-progress' + (problem ? ' has-problem' : '')} aria-label="风格构思进展">
    <div className="feedback-title"><span>{active ? <LoaderCircle size={15} className="feedback-spin"/> : problem ? <CircleAlert size={15}/> : done ? <Check size={15}/> : <Sparkles size={15}/>}推荐创作风格</span>
      <small>{active ? (task.status === 'queued' ? '已等待 ' : '已用时 ') + clock : done ? '已完成' : problem ? '需要处理' : '已停止'}</small>
    </div>
    <p className="style-progress-message" role="status">{message}</p>
    {detailed && <>
        <ol className="style-progress-steps" aria-label="风格推荐阶段">{['读取原文', '构思方向', '展开方案', '检查完成'].map((label, index) =>
          <li key={label} className={index < step ? 'is-done' : index === step && active ? 'is-current' : ''} aria-current={index === step && active ? 'step' : undefined}><span>{index < step ? <Check size={12}/> : index + 1}</span>{label}</li>)}</ol>
      <div className={'style-thinking' + (active ? ' is-live' : '')}>
        <div className="style-thinking-label"><Sparkles size={13}/><span>构思摘要</span><small>{active && live?.summary ? '正在更新' : done ? live?.summary ? '已保存' : '本次未提供' : problem ? '保留的草稿' : '等待模型返回'}</small></div>
        <StreamRegion active={active} streamKey={task.id} className="style-thinking-body" tabIndex={live?.summary ? 0 : undefined} aria-label="模型生成的构思摘要">
          {live?.summary ? <p><StreamText text={live.summary} active={active} streamKey={task.id + ':summary'} instant={problem}/></p>
            : <p className="style-thinking-empty">{problem ? '这次尚未收到可展示的构思内容。' : done ? '本次模型未提供构思摘要，可直接查看下方方案。' : '模型会先概括故事气质与画风方向，再逐个展开方案。返回的内容会显示在这里。'}</p>}
        </StreamRegion>
      </div>
      <p className="style-stream-note">{done ? '方案已完成检查，选择喜欢的方向即可填入下方。' : problem ? '已收到的内容保留为草稿，尚不能作为完整方案使用。' : connection === 'fallback' ? '实时连接暂时中断，正在定期同步已保存的进展。' : phase === 'buffered' ? '本次模型一次性返回内容，正在完成检查。' : now - (live?.updated_at || task.created || now) > 20 ? '还在等待模型返回下一段内容，已收到的内容会保留。' : live?.options?.length ? '方案正在展开，检查完成后即可选择。' : '收到构思内容后会自动更新，可以随时离开再回来。'}</p>
      <details className="style-event-history"><summary>查看处理记录 <span>{live?.events?.length || 0} 条</span></summary>

        <ol>{live?.events?.map((event, index) => <li key={index}><time>{Math.max(0, Math.floor(event.at - live.started_at))} 秒</time><span>{event.message}</span></li>)}</ol>
        {!live?.events?.length && <p className="style-compact-note">本次任务尚无处理记录。</p>}
      </details>
    </>}
    {!detailed && active && <p className="style-compact-note">{live?.options?.length ? '已收到 ' + live.options.length + ' 个方案的内容 · 构思区正在同步' : live?.summary ? '已收到构思摘要 · 正在展开方案' : '构思内容返回后会显示在上方风格区'}</p>}
  </section>;
}
