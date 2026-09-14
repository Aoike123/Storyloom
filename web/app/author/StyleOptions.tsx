'use client';

import {Check} from 'lucide-react';
import {useState} from 'react';
import type {ProgressTask} from '../ProgressFeedback';
import StreamText from '../StreamText';

type Option = {art?: string; tone?: string; reason?: string; prompt?:string};
export default function StyleOptions({task, options, art, tone, onSelect}: {task?: ProgressTask; options: Option[]; art: string; tone: string; onSelect: (art: string, tone: string) => void}) {
  const [copy,setCopy]=useState<{prompt:string;message:string}|null>(null);
  const active = !!task && ['queued', 'running', 'waiting'].includes(task.status);
  const live = task?.result?.live;
  const draft = active || (!!live && task?.status !== 'completed');
  const items = live && (draft || live.phase === 'ready') ? live.options : active ? [] : options;
  const count = active ? Math.max(3, items.length) : items.length;
  return <div className="author-options" aria-label={draft ? '正在生成的电影视觉草稿' : '可选电影视觉方案'}>
    {Array.from({length: count}, (_, index) => {
      const option = items[index];
      const selected = !draft && !!option && (art === option.prompt || art === option.art) && tone === option.tone;
      const selectable = !draft && !!option?.art && !!option?.tone;
      return <article key={index} className="author-option-shell"><button className={'author-option' + (selected ? ' is-selected' : '') + (draft ? ' is-draft' : '') + (!option ? ' is-skeleton' : '')}
        disabled={!selectable} aria-pressed={selected} onClick={() => {if (selectable) onSelect(option!.prompt || option!.art!, option!.tone!);}}>
        <span>方案 {String(index + 1).padStart(2, '0')}{selected ? <Check size={15}/> : draft ? <em>{option ? active ? '正在展开' : '未完成草稿' : '等待构思'}</em> : null}</span>
        <strong>{option?.art || <i className="style-option-placeholder" aria-hidden="true"/>}</strong>
        <p>{option?.tone || <i className="style-option-placeholder" aria-hidden="true"/>}</p>
        <small>{option?.reason ? <StreamText text={option.reason} active={active} streamKey={(task?.id || 'saved') + ':reason:' + index} instant={!active && draft}/> : <i className="style-option-placeholder is-paragraph" aria-hidden="true"/>}</small>
      </button>{option?.prompt&&<details className="style-choice-prompt" name="style-option-prompts"><summary>电影视觉提示词{draft?' · 草稿':''}</summary><pre>{option.prompt}</pre>
        {!draft&&<button type="button" onClick={async()=>{try{await navigator.clipboard.writeText(option.prompt!);setCopy({prompt:option.prompt!,message:'已复制'});}catch{setCopy({prompt:option.prompt!,message:'请选中上方文字复制'});}}}>复制提示词</button>}
        {copy?.prompt===option.prompt&&<small role="status">{copy.message}</small>}
      </details>}</article>;
    })}
  </div>;
}
