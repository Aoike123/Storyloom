'use client';
import {useEffect,useState} from 'react';
import GenerationPrompt from '../GenerationPrompt';

type Attempt={director_id?:string;stage:string;art?:string;run_id?:string;saved_at:number;invalidated_from?:string};
export default function AttemptHistory({attempts}:{attempts:Attempt[]}) {
  const [selected,setSelected]=useState(''),[data,setData]=useState<any>(null),[error,setError]=useState('');
  useEffect(()=>{
    if(!selected)return;
    const controller=new AbortController();setData(null);setError('');
    fetch('/api/director/projects/'+encodeURIComponent(selected)+'/history',{signal:controller.signal})
      .then(async r=>{if(!r.ok)throw Error('无法读取历史素材');return r.json();})
      .then(d=>{if(!controller.signal.aborted)setData(d);})
      .catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    return()=>controller.abort();
  },[selected]);
  if(!attempts.length)return null;
  return <details className="author-panel attempt-history"><summary>历史制作 · {attempts.length} 轮</summary>
    <p>上游重测后，以下结果已失效，仅供查看和对比。</p>
    <div className="production-task-tabs">{attempts.map((attempt,index)=><button type="button" key={attempt.run_id||attempt.director_id||index} disabled={!attempt.director_id} aria-pressed={selected===attempt.director_id} onClick={()=>setSelected(attempt.director_id!)}>第 {index+1} 轮 · 已失效</button>)}</div>
    {error&&<p role="alert">{error}</p>}{selected&&!data&&!error&&<p>正在读取保留的素材…</p>}
    {data&&<div className="production-output-rail">{(data.media||[]).map((asset:any)=>{
      const task=(data.tasks||[]).find((t:any)=>t.id===asset.source_task||t.result?.asset_id===asset.id||t.result?.clip_id===asset.id);
      return <article key={asset.id}><header>{asset.name||asset.title||'历史素材'}</header>{task?.kind==='video'?<video src={asset.media} controls preload="metadata"/>:<a href={asset.media} target="_blank" rel="noreferrer"><img src={asset.media} alt={asset.name||asset.title||'历史素材'} loading="lazy"/></a>}<GenerationPrompt generation={task?.generation}/></article>;
    })}</div>}
  </details>;
}
