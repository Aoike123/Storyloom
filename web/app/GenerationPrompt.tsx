'use client';
import {useState} from 'react';
import './generation-prompt.css';
import type {SkillBinding} from './NodeSkills';

export type GenerationInfo = {
  prompt: string; source: 'request' | 'task'; model?: string; image_size?: string;
  reference_count?: number; asset_kind?: string; asset_schema?: string;
  input_mode?:'reference_images'|'text'|'image_edit'|'text_to_image';
  edit_instruction?:string;
  revision_instruction?:string;regeneration_mode?:'full_prompt';
  inference_steps?:number;
  rendering_style?:Record<string,string>;reference_roles?:string[];style_reference_media?:string;
  character_key?: string | null; costume_key?: string | null; identity_asset_id?: string | null; costume_asset_id?: string | null;
  node_skill?:SkillBinding | null;
};

export default function GenerationPrompt({generation, pending = false, group}: {generation?: GenerationInfo | null; pending?: boolean; group?:string}) {
  const [copied, setCopied] = useState('');
  if (!generation?.prompt) return null;
  const kind = ({character_sheet:'人物身份图',costume_sheet:'独立服装图',scene_sheet:'场景设定图',character_costume_reference:'人物服装拼接参考图'} as Record<string,string>)[generation.asset_kind || ''] || '';
  return <details className="generation-prompt" name={group}>
    <summary>{generation.source === 'request' ? '实际请求提示词' : pending ? '待发送提示词' : '任务保存的提示词'}</summary>
    <div className="generation-prompt-body">
    {generation.edit_instruction&&<div className="generation-prompt-meta"><strong>本次修改：</strong>{generation.edit_instruction}</div>}
    {generation.revision_instruction&&<div className="generation-prompt-meta"><strong>本次修改：</strong>{generation.revision_instruction}<br/>已整合完整提示词重新生成</div>}
    {(kind || generation.model) && <div className="generation-prompt-meta">{[kind, generation.model, generation.image_size].filter(Boolean).join(' · ')}</div>}
    {generation.input_mode==='reference_images'&&<div className="generation-prompt-meta">图片参考制作 · {generation.reference_count} 张参考图</div>}
    {generation.input_mode==='image_edit'&&<div className="generation-prompt-meta">图片编辑 · {generation.reference_count} 张参考图</div>}
    {generation.inference_steps!==undefined&&<div className="generation-prompt-meta">参考合成质量参数：{generation.inference_steps} 步</div>}
    {generation.rendering_style&&<div className="generation-prompt-meta"><strong>保留基础质感：</strong>{Object.values(generation.rendering_style).join(' · ')}{generation.style_reference_media?.startsWith('/media/')&&<><br/><a href={generation.style_reference_media} target="_blank" rel="noreferrer">查看基础质感参考图</a></>}</div>}
    {!!generation.reference_roles?.length&&<div className="generation-prompt-meta">{generation.reference_roles.map((role,index)=><div key={index}>{role}</div>)}</div>}
    {(generation.character_key || generation.costume_key) && <div className="generation-prompt-meta">{[generation.character_key && '身份 '+generation.character_key,generation.costume_key && '服装 '+generation.costume_key].filter(Boolean).join(' · ')}</div>}
    {generation.node_skill&&<div className="generation-prompt-meta">专业节点：{generation.node_skill.title} · v{generation.node_skill.version}<br/>{generation.node_skill.sources.map(source=>source.name).join(' + ')}</div>}
    <pre tabIndex={0} aria-label="完整生成提示词">{generation.prompt}</pre>
    </div>
    <button type="button" onClick={async () => {
      try {await navigator.clipboard.writeText(generation.prompt); setCopied('已复制');}
      catch {setCopied('请选中上方文字复制');}
    }}>复制完整提示词</button><span role="status">{copied}</span>
    {generation.source === 'task' && !pending && <small>历史任务未保存请求快照，以上为该任务当时保存的提示词。</small>}
  </details>;
}
