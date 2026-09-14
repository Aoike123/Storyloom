'use client';
import {useEffect,useState} from 'react';
import './node-skills.css';

export type SkillBinding = {
  node:string; title:string; version:string; mode:'llm'|'template'|'human'; sha256:string; adapter:string;
  sources:{name:string;repo:string;commit:string;url:string;sections?:string[]}[];
  checklist?:string[];
};
export type SkillCall = SkillBinding & {status:'running'|'completed'|'reused'|'failed'; started_at:number; finished_at?:number};

export function SkillCallTrace({calls=[]}:{calls?:SkillCall[]}) {
  if(!calls.length)return null;
  return <details className="node-skill-trace"><summary>本任务专业节点 · {calls.length} 条记录</summary>
    {calls.map((call,index)=><div key={index}><strong>{call.title}</strong><span>{{running:'调用中',completed:'已返回',reused:'复用已保存结果',failed:'调用失败'}[call.status]}</span>
      <small>{call.sources.map(source=>source.name).join(' + ')} · 适配 v{call.version} · {call.sha256.slice(0,8)}</small></div>)}
  </details>;
}

export default function NodeSkillsPanel() {
  const [open,setOpen]=useState(false),[nodes,setNodes]=useState<SkillBinding[]>([]),[error,setError]=useState('');
  useEffect(()=>{
    if(!open||nodes.length)return;
    const controller=new AbortController();
    setError('');
    fetch('/api/node-skills',{signal:controller.signal}).then(async response=>{if(!response.ok)throw Error('无法读取节点配置');return response.json();})
      .then(data=>{if(!Array.isArray(data.nodes))throw Error('节点配置格式无效');if(!controller.signal.aborted)setNodes(data.nodes);}).catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    return()=>controller.abort();
  },[open,nodes.length]);
  return <details className="node-skills-panel" onToggle={event=>setOpen(event.currentTarget.open)}><summary>查看工作流节点与专业 Skill 绑定</summary>
    <p>这里显示已配置的专业能力；实际调用及版本记录在各任务中。点击节点名称可以检查生效的节点契约。</p>
    {error&&<p role="alert">{error}</p>}
    {open&&!nodes.length&&!error&&<p>正在读取节点配置…</p>}
    <div className="node-skills-table">{nodes.filter(node=>!['scene_trial','fitting'].includes(node.node)).map(node=><article key={node.node}>
      <a href={'/api/node-skills/'+node.node} target="_blank" rel="noreferrer">{node.title}</a>
      <span>{{llm:'模型节点',template:'请求编排模板',human:'人工验收'}[node.mode]} · v{node.version}</span>
      <small>{node.sources.map((source,index)=><a key={source.name} href={source.url} target="_blank" rel="noreferrer">{index?' + ':''}{source.name}</a>)}</small>
    </article>)}</div>
  </details>;
}
