'use client';
import BrainstormDirector from './BrainstormDirector';
import {useEffect,useState,useRef} from 'react';
type Item={work_id:string,title?:string,description?:string,labels?:unknown};
type Listing={items:Item[],cached:boolean,stale:boolean,warning?:string,fetched_at:number};
type Detail={raw:{chapter_name?:string,author_name?:string,introduction?:string,content?:string,labels?:unknown},listing:Item,stale:boolean,warning?:string};
type Saved={id:string,title:string,author_name?:string,work_id:string,content:string,labels?:unknown,fetched_at:number,raw:Detail['raw'],listing:Item};
const labels=(value:unknown):string[]=>Array.isArray(value)?value.filter((x):x is string=>typeof x==='string'):[];
async function request<T>(path:string,post=false):Promise<T>{const r=await fetch('/api/stories'+path,post?{method:'POST'}:undefined);const d=await r.json();if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'读取失败');return d;}
export default function StoryLibrary(){
 const [list,setList]=useState<Listing|null>(null),[detail,setDetail]=useState<Detail|null>(null),[selected,setSelected]=useState(''),[query,setQuery]=useState(''),[tag,setTag]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const [saved,setSaved]=useState<Saved[]>([]),[view,setView]=useState<'catalog'|'saved'>('catalog'),[opened,setOpened]=useState(false),[localId,setLocalId]=useState('');
 const heading=useRef<HTMLHeadingElement>(null);
 useEffect(()=>{if(opened){heading.current?.scrollIntoView({block:'start'});heading.current?.focus();}},[opened]);
 async function loadSaved(){try{setSaved(await request<Saved[]>('/imports'));}catch(e){setError((e as Error).message);}}
 async function load(refresh=false){setBusy(true);setError('');try{setList(await request<Listing>(refresh?'?refresh=true':''));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 useEffect(()=>{load();loadSaved();},[]);
 const tags=Array.from(new Set(list?.items.flatMap(x=>labels(x.labels))||[])).sort();
 async function choose(item:Item){setOpened(true);setLocalId('');setBusy(true);setError('');setNotice('');setDetail(null);setSelected(item.work_id);try{setDetail(await request<Detail>('/'+encodeURIComponent(item.work_id)));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 return <div className="page"><div className="page-heading"><div><div className="eyebrow">ZHIHU STORY LIBRARY</div><h1 ref={heading} tabIndex={-1}>{opened?'故事详情':'知乎故事库'}</h1><p>直接读取赛方故事与标签；选择作品后才获取正文。</p></div><button className="button secondary" disabled={busy} onClick={()=>opened?setOpened(false):load(true)}>{opened?'返回故事库':'刷新赛方列表'}</button></div>
 {error&&<p role="alert" className="error-banner">{error}</p>}{notice&&<p role="status">{notice}</p>}
 {!opened&&<><div style={{display:'flex',gap:12,marginBottom:20}}><button className={'button '+(view==='catalog'?'primary':'secondary')} onClick={()=>setView('catalog')}>赛方故事</button><button className={'button '+(view==='saved'?'primary':'secondary')} onClick={()=>{setView('saved');loadSaved();}}>已保存故事（{saved.length}）</button></div>
 {view==='saved'?<><p>故事保存在本机工作台数据库中，可在这里重新打开；不会另存为下载文件。</p>{saved.length===0&&<p>暂无已保存故事。打开一篇赛方故事，点击“保存故事到本地工作台”。</p>}<div className="clips-grid">{saved.map(x=><article className="clip-card" key={x.id}><div className="clip-info"><h3>{x.title}</h3><p>作者：{x.author_name||'接口未提供'} · {labels(x.labels).join(' · ')}</p><p>来源获取时间：{new Date(x.fetched_at*1000).toLocaleString('zh-CN')}</p><button className="button secondary" onClick={()=>{setError('');setNotice('');setSelected(x.work_id);setLocalId(x.id);setDetail({raw:{...x.raw,content:x.content},listing:x.listing,stale:false});setOpened(true);}}>打开已保存故事</button></div></article>)}</div></>:<>
 {list&&<p>{list.items.length} 篇作品 · {list.stale?'接口暂不可用，显示旧缓存':list.cached?'来自本地缓存':'已从知乎获取'} · {new Date(list.fetched_at*1000).toLocaleString('zh-CN')}</p>}{list?.warning&&<p role="alert">{list.warning}</p>}
 <div style={{display:'flex',gap:16,marginBottom:20}}><input className="text-input" aria-label="搜索故事" placeholder="搜索标题或摘要" value={query} onChange={e=>setQuery(e.target.value)}/><select aria-label="筛选故事标签" value={tag} onChange={e=>setTag(e.target.value)}><option value="">全部标签</option>{tags.map(t=><option key={t}>{t}</option>)}</select></div>
 <div className="clips-grid">{list?.items.filter(x=>(!tag||labels(x.labels).includes(tag))&&((x.title||'')+(x.description||'')).includes(query)).map(x=><article className="clip-card" key={x.work_id}><div className="clip-info"><h3>{x.title||'接口未提供标题'}</h3><p>{labels(x.labels).join(' · ')||'暂无标签'}</p><p>{x.description||'暂无摘要'}</p><button className="button secondary" disabled={busy} onClick={()=>choose(x)}>查看故事</button></div></article>)}</div>
 </>}</>}
 {busy&&<p role="status">正在读取…</p>}
 {opened&&detail&&<section className="config-guide" style={{marginTop:24}}><h2>{detail.raw.chapter_name||detail.listing.title}</h2><p>作者：{detail.raw.author_name||'接口未提供'} · 来源：知乎黑客松故事 API</p><p>{labels(detail.raw.labels??detail.listing.labels).join(' · ')}</p><p className="info-strip">接口未标明正文是否完整。导入仅保存来源版本，不自动认定作者结局或发布视频。</p>{detail.stale&&<p role="alert">正在显示缓存正文。{detail.warning}</p>}<p>{detail.raw.introduction}</p><div style={{whiteSpace:'pre-wrap',maxHeight:480,overflowY:'auto',lineHeight:1.9}}>{typeof detail.raw.content==='string'?detail.raw.content:'接口未提供正文'}</div><button className="button primary" style={{marginTop:20}} disabled={busy||!detail.raw.content||!!localId} onClick={async()=>{setBusy(true);setError('');try{const result=await request<{story:Saved}>('/'+encodeURIComponent(selected)+'/import',true);setLocalId(result.story.id);await loadSaved();setNotice('保存成功。可从“知乎故事库 → 已保存故事”重新打开。');}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>{localId?'已保存到本地工作台':'保存故事到本地工作台'}</button>{localId&&<button className="button secondary" style={{marginLeft:12}} onClick={()=>{setView('saved');setOpened(false);setNotice('');}}>前往已保存故事</button>}</section>}
 {opened&&localId&&labels(detail?.raw.labels??detail?.listing.labels).includes('脑洞')&&<BrainstormDirector key={localId} sourceId={localId}/>}
 </div>;
}
