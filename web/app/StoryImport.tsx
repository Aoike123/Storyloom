'use client';
import {useEffect, useRef, useState} from 'react';
import {ArrowRight, FileUp, PenLine, X} from 'lucide-react';
import {uiErrorMessage} from './ui-errors';
import './story-import.css';

// The title field promises 「不填则用正文首行」 — keep that promise here.
export function firstLineTitle(text: string) {
  return text.split('\n').map(line => line.trim()).find(Boolean)?.slice(0, 200);
}

export default function StoryImport({onImported, open, onClosed}:{onImported?:(storyId?:string)=>void; open:boolean; onClosed?:()=>void}) {
  const [tab, setTab] = useState<'paste' | 'file'>('paste');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef(0);

  function revealPanel() {
    const panel = containerRef.current;
    if (!panel) return;
    const bounds = panel.getBoundingClientRect();
    if (bounds.top < 28 || bounds.bottom > window.innerHeight - 28) {
      panel.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth',block:'start'});
    }
  }

  useEffect(() => {
    if (!open) return;
    const id = window.requestAnimationFrame(() => {
      const panel = containerRef.current;
      panel?.querySelector<HTMLElement>('input:not([type=file]), textarea')?.focus({preventScroll:true});
      if (window.matchMedia('(prefers-reduced-motion:reduce)').matches) revealPanel();
    });
    return () => window.cancelAnimationFrame(id);
  }, [open]);
  useEffect(() => () => window.clearTimeout(closeTimer.current), []);

  function after(storyId: string, name: string) {
    setContent(''); setTitle(''); setFile(null); if (fileRef.current) fileRef.current.value = '';
    setDone(`「${name}」已导入故事市场，可以开始制作你的漫剧版本。`);
    onImported?.(storyId);
    closeTimer.current = window.setTimeout(() => onClosed?.(), 1200);
  }

  async function submit() {
    setError(''); setDone('');
    setBusy(true);
    try {
      let d: any;
      if (tab === 'paste') {
        if (!content.trim()) { setError('请粘贴故事正文。'); setBusy(false); return; }
        const r = await fetch('/api/stories/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: title.trim() || firstLineTitle(content) || undefined, content})});
        d = await r.json();
        if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '导入失败，请重试。');
        after(d.story?.id, d.story?.title || title.trim() || '这个故事');
      } else {
        if (!file) { setError('请选择一个文本文件。'); setBusy(false); return; }
        const form = new FormData();
        form.append('file', file);
        if (title.trim()) form.append('title', title.trim());
        const r = await fetch('/api/stories/import-file', {method: 'POST', body: form});
        d = await r.json();
        if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '导入失败，请重试。');
        after(d.story?.id, d.story?.title || file.name);
      }
    } catch (e) {
      setError(uiErrorMessage(e, '导入失败，请重试。'));
    } finally {
      setBusy(false);
    }
  }

  return <div ref={containerRef} className="story-import" role="dialog" aria-label="导入故事" onTransitionEnd={event=>{if(open&&event.target===event.currentTarget&&event.propertyName==='transform')revealPanel();}}>
    <div className="story-import-head">
      <div><span className="story-import-kicker">从文字，到画面</span><strong>导入一篇原作</strong><p>粘贴正文或上传文本，故事从这里开始。</p></div>
      <button type="button" className="story-import-close button secondary icon" aria-label="关闭" disabled={busy} onClick={() => { setDone(''); setError(''); onClosed?.(); }}>
        <X size={15}/>
      </button>
    </div>
    <div className="story-import-tabs" role="tablist" aria-label="导入方式">
      <button type="button" role="tab" aria-selected={tab === 'paste'} className={'button tab-control'+(tab === 'paste' ? ' selected' : '')} onClick={() => {setTab('paste');setError('');setDone('');}}><PenLine size={15}/>粘贴正文</button>
      <button type="button" role="tab" aria-selected={tab === 'file'} className={'button tab-control'+(tab === 'file' ? ' selected' : '')} onClick={() => {setTab('file');setError('');setDone('');}}><FileUp size={15}/>上传文件</button>
    </div>
    {tab === 'paste'
      ? <div className="story-import-field" key="paste">
        <label><span>故事标题<small>（可选）</small></span><input aria-label="故事标题（可选）" value={title} maxLength={200} onChange={e => setTitle(e.target.value)} placeholder="不填则用正文首行"/><small className="story-import-hint">原文会完整保留，作为后续创作的起点。</small></label>
        <label>正文<textarea rows={8} value={content} onChange={e => setContent(e.target.value)} placeholder="把微小说的正文粘贴到这里…"/></label>
      </div>
      : <div className="story-import-field" key="file">
        <label><span>故事标题<small>（可选）</small></span><input aria-label="故事标题（可选）" value={title} maxLength={200} onChange={e => setTitle(e.target.value)} placeholder="不填则用文件名"/><small className="story-import-hint">原文会完整保留，作为后续创作的起点。</small></label>
        <label>文本文件<span className="story-file-zone"><input type="file" ref={fileRef} aria-label="选择文本文件" accept=".txt,.md,text/plain" onChange={e => setFile(e.target.files?.[0] || null)}/><FileUp size={30} strokeWidth={1.4} aria-hidden="true"/><strong>{file ? '已选择文本文件' : '选择一个文本文件'}</strong><span>支持 .txt 和 .md</span></span>{file&&<small className="story-import-hint">{file.name}</small>}</label>
      </div>}
    <div className="story-import-actions">
      <span>导入后，可以在故事市场开始制作。</span>
      <button type="button" className="button primary" disabled={busy} onClick={submit}>{busy ? '正在导入…' : '导入故事市场'}<ArrowRight size={15} className="button-arrow"/></button>
    </div>
    <div className="story-import-feedback">
      {error && <div className="story-import-error" role="alert">{error}</div>}
      {done && <div className="story-import-done" role="status">{done}</div>}
    </div>
  </div>;
}
