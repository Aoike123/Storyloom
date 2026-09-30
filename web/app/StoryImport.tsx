'use client';
import {useEffect, useRef, useState} from 'react';
import {FileUp, PenLine, X} from 'lucide-react';
import './story-import.css';

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

  useEffect(() => {
    if (!open) return;
    const id = window.requestAnimationFrame(() => {containerRef.current?.querySelector<HTMLElement>('input:not([type=file]), textarea')?.focus();});
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
        const r = await fetch('/api/stories/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: title.trim() || undefined, content})});
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
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <div ref={containerRef} className="story-import" role="dialog" aria-label="导入故事">
    <div className="story-import-head">
      <div><strong>导入一篇原作</strong><span>粘贴正文或上传文本文件，导入后即可在故事市场里制作漫剧。</span></div>
      <button type="button" className="story-import-close" aria-label="关闭" disabled={busy} onClick={() => { setDone(''); setError(''); onClosed?.(); }}>
        <X size={15}/>
      </button>
    </div>
    <div className="story-import-tabs" role="tablist">
      <button type="button" role="tab" aria-selected={tab === 'paste'} className={tab === 'paste' ? 'selected' : ''} onClick={() => setTab('paste')}><PenLine size={14}/>粘贴正文</button>
      <button type="button" role="tab" aria-selected={tab === 'file'} className={tab === 'file' ? 'selected' : ''} onClick={() => setTab('file')}><FileUp size={14}/>上传文件</button>
    </div>
    {tab === 'paste'
      ? <div className="story-import-field">
        <label>标题<span>（可选）</span><input value={title} maxLength={200} onChange={e => setTitle(e.target.value)} placeholder="不填则用正文首行"/></label>
        <label>正文<textarea rows={8} value={content} onChange={e => setContent(e.target.value)} placeholder="把微小说的正文粘贴到这里…"/></label>
      </div>
      : <div className="story-import-field">
        <label>标题<span>（可选）</span><input value={title} maxLength={200} onChange={e => setTitle(e.target.value)} placeholder="不填则用文件名"/></label>
        <label>文本文件<input type="file" ref={fileRef} accept=".txt,.md,text/plain" onChange={e => setFile(e.target.files?.[0] || null)}/>{file && <small>{file.name}</small>}</label>
      </div>}
    {error && <div className="story-import-error" role="alert">{error}</div>}
    {done && <div className="story-import-done" role="status">{done}</div>}
    <div className="story-import-actions">
      <button type="button" className="button primary" disabled={busy} onClick={submit}>{busy ? '正在导入…' : '导入故事市场'}</button>
    </div>
  </div>;
}
