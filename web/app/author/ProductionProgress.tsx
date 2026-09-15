'use client';
import {useState} from 'react';
import {ArrowDown, Check, Clapperboard, ImageIcon} from 'lucide-react';
import {TaskProgress, activeStatuses, taskNames} from '../ProgressFeedback';
import type {ProgressTask} from '../ProgressFeedback';
import GenerationPrompt from '../GenerationPrompt';
import type {GenerationInfo} from '../GenerationPrompt';

export type ProductionOutput = {id: string; kind: string; title: string; media: string; production_phase?: string; generation?: GenerationInfo | null};
type Node = {id:string;name:string;hint:string;task?:ProgressTask|null};
export default function ProductionProgress({tasks, outputs = [], stage, connection, phase, node}: {tasks: ProgressTask[]; outputs?: ProductionOutput[]; stage: string; connection: string; phase?:string;node?:Node}) {
  const [selected, setSelected] = useState('');
  const split = ['storyboarding','rendering'].includes(phase || '');
  const scoped = split && tasks.some(task=>task.production_phase===phase);
  const relevant = tasks.filter(task => task.kind !== 'author_styles' && (!scoped || task.production_phase === phase));
  const allConcrete = relevant.filter(task => !['author_flow','author_composite','author_storyboard','author_render','creative_watch'].includes(task.kind));
  const visibleOutputs = scoped ? outputs.filter(output=>output.production_phase===phase) : outputs;
  const replaced = new Set([...allConcrete.map(task => task.revision_of), ...allConcrete.flatMap(task => task.result?.replaced_assets || [])].filter(Boolean));
  const isCurrent=(task:ProgressTask)=>!['cancelled','superseded'].includes(task.status);
  const concrete = allConcrete.filter(task=>!replaced.has(task.id)&&isCurrent(task));
  const active = concrete.filter(task => activeStatuses.includes(task.status));
  const model = active.find(task => task.activity?.type === 'model');
  const focused = concrete.find(task => task.id === selected) || model || active[0] || concrete.at(-1) || relevant.find(isCurrent);
  const latest = concrete.slice(-16);
  const media = concrete.filter(task => (task.kind === 'image' || task.kind === 'video') && !replaced.has(task.id));
  const ready = media.filter(task => task.status === 'completed').length;
  const waiting = relevant.some(task => activeStatuses.includes(task.status));
  if (!relevant.some(isCurrent) && !['preparing','producing','storyboarding','rendering'].includes(stage)) return null;
  return <section className="production-progress" aria-label="本轮制作进展">
    <header><div><span className="studio-eyebrow">{node?.hint || (waiting ? '故事正在成形' : '本轮制作记录')}</span><h3>{node?.name || (waiting ? '可以边等，边看看。' : '这轮制作，进展到这里。')}</h3></div><small>{connection === 'fallback' ? '正在定期同步' : connection === 'live' ? '进展实时同步' : '正在连接进展'}</small></header>
    {node && <p className="studio-note">{node.task?.status==='completed'?'本节点已完成，结果单独保存。':node.task&&['failed','needs_review'].includes(node.task.status)?'问题停留在本节点，上游已完成结果会保留。':'本节点完成后自动进入下一步；这里只显示本节点的任务与结果。'}</p>}
    {latest.length > 1 && <div className="production-task-tabs" aria-label="查看各项制作进展">{latest.map(task => <button key={task.id} aria-pressed={focused?.id === task.id} onClick={() => setSelected(task.id)}>{task.status === 'completed' && <Check size={11}/>}<span>{task.kind === 'image' ? '画面 · ' : task.kind === 'video' ? '片段 · ' : ''}{task.label || taskNames[task.kind] || '制作任务'}</span></button>)}</div>}
    {focused ? <TaskProgress task={focused} detailed/> : <div className="production-starting" role="status"><p>制作请求已收到，正在准备原文与参考设定。</p><small>模型返回的创作摘要和已完成的素材会出现在这里。</small></div>}
    {phase==='storyboarding' ? <p className="studio-note">分镜方案和镜头提示词会保存在上方任务记录中。本节点不生成图片或视频。</p> : <><div className="production-output-heading"><span>{media.length ? '视频片段 · ' + ready + ' / ' + media.length + ' 已完成' : '视频片段'}</span><a href="#production-outputs">看看已完成的素材 <ArrowDown size={12}/></a></div>
    <div id="production-outputs" className="production-output-rail" aria-label="已完成的素材预览">
      {visibleOutputs.length ? visibleOutputs.map(output => <article key={output.id}><header>{output.kind === 'video' ? <Clapperboard size={13}/> : <ImageIcon size={13}/>}<span>{output.title}</span></header>
        {output.kind === 'video' ? <video src={output.media} controls playsInline preload="metadata"/> : <a href={output.media} target="_blank" rel="noreferrer" aria-label={'查看完整画面：' + output.title}><img src={output.media} alt={output.title} loading="lazy"/></a>}
        <small>已保存 · {output.kind === 'video' ? '可提前观看' : '可查看完整画面'}</small>
        <GenerationPrompt generation={output.generation} group="production-output-prompts"/>
      </article>) : <div className="production-output-empty"><ImageIcon size={22}/><p>{waiting ? '第一份素材完成后，就能在这里看到。' : '本轮尚无已完成的画面或片段。'}</p><small>现在可以阅读左侧原文，或查看上方创作内容。</small></div>}
    </div></>}
  </section>;
}
