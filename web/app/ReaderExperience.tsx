'use client';

import Link from './ReaderLink';
import {useEffect, useRef, useState} from 'react';
import {ArrowRight, Clapperboard, LoaderCircle, Search, Sparkles} from 'lucide-react';
import {InteractionFeedback, workStages} from './ProgressFeedback';
import ReaderArtwork from './ReaderArtwork';
import ReaderHero from './ReaderHero';
import ReaderStoryGrid from './ReaderStoryGrid';
import {canWatch, productionLink} from './reader-types';
import type {Catalog, CatalogItem, Release} from './reader-types';
import './reader-catalog.css';
import './reader-motion.css';

type Wish = {at: number; text: string};
type BrowseState = {query: string; filter: string; featuredIds: string[]; activeId: string; scrollY: number; focusKey: string};
const browseKey = 'storyloom.reader.browse.v1';

export default function ReaderExperience() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [query, setQuery] = useState(''), [searchInput, setSearchInput] = useState('');
  const [filter, setFilter] = useState('all'), [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true), [featuredIds, setFeaturedIds] = useState<string[]>([]);
  const [activeId, setActiveId] = useState('');
  const [story, setStory] = useState<Release | null>(null), [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(true), [moment, setMoment] = useState(0);
  const [text, setText] = useState(''), [wishes, setWishes] = useState<Wish[]>([]);
  const [reply, setReply] = useState(''), [replyError, setReplyError] = useState(false);
  const [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  const video = useRef<HTMLVideoElement>(null), input = useRef<HTMLTextAreaElement>(null);
  const continuePlay = useRef(false), openedLink = useRef(false), composing = useRef(false);
  const catalogRef = useRef<Catalog | null>(null), currentStory = useRef<string | null>(null);
  const requestVersion = useRef(0), drafts = useRef<Record<string, string>>({}), savedWishes = useRef<Record<string, Wish[]>>({});
  const restore = useRef(false), savedBrowse = useRef<BrowseState | null>(null);
  const browse = useRef({query, filter, featuredIds, activeId});
  browse.current = {query, filter, featuredIds, activeId};
  const entry = story?.entries[index];

  function remember(focusKey = '', selectedId?: string) {
    const state = {...browse.current, activeId: selectedId || browse.current.activeId, scrollY: window.scrollY, focusKey};
    savedBrowse.current = state;
    try {sessionStorage.setItem(browseKey, JSON.stringify(state));} catch {}
  }

  function showStory(release: Release) {
    video.current?.pause();
    currentStory.current = release.id;
    requestVersion.current++;
    continuePlay.current = false;
    setStory(release); setIndex(0); setMoment(release.entries[0].start); setPaused(true);
    setText(drafts.current[release.id] || ''); setWishes(savedWishes.current[release.id] || []);
    setReply(''); setReplyError(false); setMessage(''); setBusy(false);
    window.scrollTo({top: 0, behavior: 'instant'});
  }

  function returnToCatalog() {
    video.current?.pause();
    currentStory.current = null;
    requestVersion.current++;
    continuePlay.current = false;
    restore.current = true;
    setStory(null); setBusy(false);
  }

  function back() {
    if (window.history.state?.readerWatch) window.history.back();
    else {
      const url = new URL(window.location.href);
      url.searchParams.delete('story');
      window.history.replaceState(window.history.state, '', url);
      returnToCatalog();
    }
  }

  function openItem(item: CatalogItem, focusKey: string) {
    remember(focusKey, focusKey.startsWith('hero:') ? item.id : undefined);
    if (!canWatch(item)) return;
    const url = new URL(window.location.href);
    url.searchParams.set('story', item.release!.id); url.hash = '';
    window.history.pushState({...window.history.state, readerWatch: true}, '', url);
    showStory(item.release!);
  }

  useEffect(() => {
    try {
      const state = JSON.parse(sessionStorage.getItem(browseKey) || 'null');
      if (state && typeof state.query === 'string' && ['all', 'ready', 'pending'].includes(state.filter)) {
        const restored: BrowseState = {
          query: state.query, filter: state.filter,
          featuredIds: Array.isArray(state.featuredIds) ? state.featuredIds.filter((id: unknown) => typeof id === 'string').slice(0, 3) : [],
          activeId: typeof state.activeId === 'string' ? state.activeId : '',
          scrollY: Number.isFinite(state.scrollY) ? Math.max(0, state.scrollY) : 0,
          focusKey: typeof state.focusKey === 'string' ? state.focusKey : '',
        };
        savedBrowse.current = restored; restore.current = true;
        setQuery(restored.query); setSearchInput(restored.query); setFilter(restored.filter);
        setFeaturedIds(restored.featuredIds); setActiveId(restored.activeId);
      }
    } catch {}
    const onPop = () => {
      const id = new URLSearchParams(window.location.search).get('story');
      const release = catalogRef.current?.releases.find(item => item.id === id && item.entries.length);
      if (release) showStory(release); else returnToCatalog();
    };
    const onLeave = () => {if (!currentStory.current) remember(savedBrowse.current?.focusKey || '');};
    window.addEventListener('popstate', onPop);
    window.addEventListener('pagehide', onLeave);
    return () => {window.removeEventListener('popstate', onPop); window.removeEventListener('pagehide', onLeave);};
  }, []);

  useEffect(() => {
    let alive = true;
    const abort = new AbortController();
    async function load(force = false) {
      try {
        const response = await fetch('/api/reader/catalog' + (force ? '?refresh=true' : ''), {signal: abort.signal});
        if (!response.ok) throw Error('暂时无法读取故事目录，请稍后重试。');
        const data: Catalog = await response.json();
        if (!alive) return;
        catalogRef.current = data; setCatalog(data); setMessage('');
        if (!openedLink.current) {
          openedLink.current = true;
          const id = new URLSearchParams(window.location.search).get('story');
          const found = data.releases.find(item => item.id === id && item.entries.length);
          if (found) showStory(found);
          else if (id) setMessage('这部作品暂时无法播放，可从目录选择其他作品。');
        }
      } catch (error) {
        if (alive) setMessage((error as Error).message);
      } finally {
        if (alive) setLoading(false);
      }
    }
    setLoading(true); load(refresh > 0);
    return () => {alive = false; abort.abort();};
  }, [refresh]);

  useEffect(() => {
    if (!catalog) return;
    const available = new Set(catalog.items.map(item => item.id));
    setFeaturedIds(previous => {
      // Keep the covers under the reader's pointer stable during background refresh.
      const retained = previous.filter(id => available.has(id));
      const ranked = [...catalog.items].sort((a, b) => Number(canWatch(b)) - Number(canWatch(a)));
      const next = [...new Set([...retained, ...ranked.map(item => item.id)])].slice(0, 3);
      return next.join('|') === previous.join('|') ? previous : next;
    });
  }, [catalog]);

  useEffect(() => {
    if (!featuredIds.includes(activeId)) setActiveId(featuredIds[0] || '');
  }, [featuredIds, activeId]);

  useEffect(() => {
    if (loading || story || !restore.current || !catalog || (catalog.items.length && !featuredIds.length)) return;
    const frame = requestAnimationFrame(() => {
      const state = savedBrowse.current;
      if (state) {
        const target = Array.from(document.querySelectorAll<HTMLElement>('[data-reader-focus]')).find(el => el.dataset.readerFocus === state.focusKey);
        target?.focus({preventScroll: true});
        window.scrollTo({top: state.scrollY, behavior: 'instant'});
      }
      restore.current = false;
    });
    return () => cancelAnimationFrame(frame);
  }, [loading, story, catalog, featuredIds]);

  function pause(focus = true) {
    video.current?.pause(); setPaused(true);
    setMoment(video.current?.currentTime ?? entry?.start ?? 0);
    if (focus) input.current?.focus({preventScroll: true});
  }

  const elapsed = story ? story.entries.slice(0, index).reduce((total, item) => total + item.end - item.start, 0) + Math.max(0, moment - (entry?.start || 0)) : 0;

  async function saveWish() {
    if (!story || !entry || busy || !paused || text.trim().length < 2) return;
    const submittedText = text.trim(), releaseId = story.id, at = video.current?.currentTime ?? entry.start;
    const submittedElapsed = elapsed, version = ++requestVersion.current;
    setBusy(true); setReply(''); setReplyError(false);
    try {
      const response = await fetch('/api/reader/wishes', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        release_id: releaseId, index, offset: Math.max(entry.start, Math.min(entry.end, at)), text: submittedText,
      })});
      const data = await response.json();
      if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '保存失败，请重试。');
      const next = [...(savedWishes.current[releaseId] || []), {at: submittedElapsed, text: submittedText}];
      savedWishes.current[releaseId] = next;
      if (drafts.current[releaseId]?.trim() === submittedText) drafts.current[releaseId] = '';
      if (currentStory.current === releaseId && requestVersion.current === version) {
        setWishes(next); setText(current => current.trim() === submittedText ? '' : current);
        setReply('想法已保存 · ' + Math.floor(submittedElapsed) + ' 秒。继续看看故事怎么演。');
      }
    } catch (error) {
      if (currentStory.current === releaseId && requestVersion.current === version) {setReply((error as Error).message); setReplyError(true);}
    } finally {
      if (currentStory.current === releaseId && requestVersion.current === version) setBusy(false);
    }
  }

  const visible = (catalog?.items || []).filter(item =>
    (filter === 'ready' ? canWatch(item) : filter === 'pending' ? !canWatch(item) : true) &&
    ((item.title || '') + ' ' + (item.description || '')).toLowerCase().includes(query.toLowerCase()));
  const featured = featuredIds.map(id => catalog?.items.find(item => item.id === id)).filter((item): item is CatalogItem => !!item);
  const clearFilters = () => {setFilter('all'); setQuery(''); setSearchInput('');};

  return <div className="reader-world">
    <nav className="reader-nav">
      <button className="reader-brand" onClick={() => {if (story) back(); else window.scrollTo({top: 0, behavior: 'instant'});}}>叙间<span>每个故事，都有另一种可能</span></button>
      <div className="demo-entry-links"><Link href="/author" prefetch={false} onClick={() => {if (!story) remember('nav:author');}} data-reader-focus="nav:author">漫剧生成 <ArrowRight size={14}/></Link></div>
    </nav>
    {!story ? <div className="reader-catalog-page">
      <ReaderHero items={featured} loading={loading || (!!catalog?.items.length && !featured.length)} activeId={activeId} onActiveChange={setActiveId} onOpen={openItem}/>
      <section id="reader-shelf" className="reader-shelf">
        <div className="reader-section-title">
          <div><span className="reader-kicker">THE STORY COLLECTION</span><h2>脑洞微小说 · 故事目录</h2></div>
          <span>{catalog ? catalog.brainstorm_count + ' 篇脑洞微小说 · ' + catalog.ready_count + ' 部已有漫剧' : '正在读取目录'}</span>
        </div>
        <div className="catalog-toolbar">
          <div className="catalog-filters" aria-label="筛选漫剧状态">{[['all', '全部故事'], ['ready', '已有漫剧'], ['pending', '待生成漫剧']].map(([value, label]) =>
            <button key={value} aria-pressed={filter === value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div>
          <label className="catalog-search"><Search size={16}/><input aria-label="搜索微小说" placeholder="搜索标题或简介" value={searchInput}
            onCompositionStart={() => {composing.current = true;}}
            onCompositionEnd={event => {composing.current = false; setQuery(event.currentTarget.value);}}
            onChange={event => {setSearchInput(event.target.value); if (!composing.current) setQuery(event.target.value);}}/></label>
          <button className="catalog-refresh" disabled={loading} onClick={() => setRefresh(value => value + 1)}>
            {loading && <LoaderCircle size={13} className="feedback-spin"/>}{loading ? '读取中…' : '刷新目录'}
          </button>
        </div>
        {catalog?.warning && <div className="catalog-warning" role="status">{catalog.stale ? '故事服务暂不可用，正在显示已保存的目录。' : '故事目录暂时无法读取，当前仅展示本地可观看的作品。'}{catalog.fetched_at && <small>目录保存于 {new Date(catalog.fetched_at * 1000).toLocaleString('zh-CN')}</small>}</div>}
        {message && <div className="catalog-warning catalog-error" role="alert">{message}<button onClick={() => setRefresh(value => value + 1)} disabled={loading}>重试读取</button></div>}
        {loading && !catalog ? <div className="reader-story-grid catalog-skeleton-grid" role="status" aria-label="正在打开脑洞故事库"><span className="feedback-sr-only">正在打开脑洞故事库</span>{[0, 1, 2].map(id =>
          <div className="reader-story-card catalog-skeleton" key={id} aria-hidden="true"><div className="catalog-poster"/><div className="catalog-story-info"><i/><i/><i/></div></div>)}</div>
          : !visible.length ? <div className="reader-empty" role="status">
            <span>{query ? '没有找到匹配的微小说。' : filter === 'ready' ? '第一部漫剧，等你来制作。' : '暂时没有可展示的故事。'}</span>
            <p>{filter === 'ready' && !query ? '切换到全部故事，选一篇微小说，体验从原文到视频的制作流程。' : '可以刷新目录或调整筛选条件。'}</p>
            {(filter !== 'all' || query) && <button onClick={clearFilters}>查看全部故事 →</button>}
          </div> : <ReaderStoryGrid ids={visible.map(item => item.id)}>{visible.map((item, order) => {
            const playable = canWatch(item), release = item.release, cover = item.artwork || item.tab_artwork;
            const stage = workStages.find(candidate => candidate.id === item.stage);
            const posterKey = 'shelf:poster:' + item.id, sourceKey = 'shelf:source:' + item.id, watchKey = 'shelf:watch:' + item.id;
            return <article className={'reader-story-card ' + (playable ? 'is-ready' : 'is-pending')} key={item.id} data-story-id={item.id}>
              <div className="catalog-poster">
                {playable ? <button aria-label={'观看《' + item.title + '》'} data-reader-focus={posterKey} onClick={() => openItem(item, posterKey)}>
                  {cover ? <ReaderArtwork src={cover} order={order}/> : <video src={release!.entries[0].media} preload="metadata" muted/>}
                  <span className="catalog-play"><Clapperboard size={22}/> 观看漫剧</span>
                </button> : <Link href={productionLink(item)} prefetch={false} aria-label={'查看《' + item.title + '》并生成漫剧'} data-reader-focus={posterKey} onClick={() => remember(posterKey)}>
                  <ReaderArtwork src={cover} order={order}/><span className="catalog-not-made">这段故事，尚未有画面</span>
                </Link>}
                <span className="catalog-status">{playable ? '已有漫剧' : stage && item.stage !== 'published' ? '制作进度 · ' + stage.name : '待生成漫剧'}</span>
              </div>
              <div className="catalog-story-info">
                <div className="catalog-tags">{item.labels.slice(0, 3).map(label => <span key={label}>{label}</span>)}</div>
                <h3>{item.title || '未提供标题'}</h3><p>{item.description || '打开微小说详情，阅读原文并选择制作风格。'}</p>
                <div className="catalog-card-actions">{playable ? <>
                  <button onClick={() => openItem(item, watchKey)} data-reader-focus={watchKey}>立即观看 <ArrowRight size={15}/></button>
                  {(item.work_id || item.project_id) && <Link href={productionLink(item)} prefetch={false} data-reader-focus={sourceKey} onClick={() => remember(sourceKey)}>原文与制作</Link>}
                </> : <Link href={productionLink(item)} prefetch={false} data-reader-focus={sourceKey} onClick={() => remember(sourceKey)}>
                  <Sparkles size={14}/>{item.project_id ? '查看原文并继续制作' : '查看微小说，生成漫剧'} <ArrowRight size={15}/>
                </Link>}</div>
              </div>
            </article>;
          })}</ReaderStoryGrid>}
        {catalog?.catalog_available && <p className="catalog-footnote">目录展示故事服务返回的全部脑洞类微小说，已有漫剧优先排列。未生成的卡片仍可进入原文与制作页面。</p>}
      </section>
    </div> : <section className="reader-screen">
      <button className="reader-back" onClick={back}>← 返回故事</button>
      <header><small>改编自《{story.source_title}》 · {story.author || '作者未提供'}</small><h1>{story.title}</h1>
        {(story.source_work_id || story.project_id) && <Link className="reader-source-link" href={productionLink(story)} prefetch={false}>阅读微小说 · 查看制作流程 <ArrowRight size={14}/></Link>}
      </header>
      <div className="reader-view-grid"><div>
        <div className="reader-cinema"><video key={story.id + ':' + index} ref={video} src={entry?.media} controls playsInline
          onLoadedMetadata={() => {if (video.current && entry) {video.current.currentTime = entry.start; if (continuePlay.current) {continuePlay.current = false; video.current.play().catch(() => setPaused(true));}}}}
          onPlay={() => setPaused(false)} onPause={() => {setPaused(true); setMoment(video.current?.currentTime ?? 0);}}
          onTimeUpdate={() => {const v = video.current; if (!v || !entry) return; setMoment(v.currentTime); if (v.currentTime >= entry.end - .04 && !v.paused) {v.pause(); if (index < story.entries.length - 1) {continuePlay.current = true; setIndex(index + 1);}}}}
          onEnded={() => {if (index < story.entries.length - 1) {continuePlay.current = true; setIndex(index + 1);}}}/>
        </div>
        <div className="reader-playbar"><span>第 {index + 1} / {story.entries.length} 段 · {Math.floor(elapsed)} 秒</span><button onClick={() => pause()}>暂停，留下另一种可能</button></div>
        <div className="reader-segments">{story.entries.map((segment, i) => <button key={segment.clip_id} className={index === i ? 'active' : ''}
          onClick={() => {pause(false); continuePlay.current = false; setIndex(i); setMoment(segment.start);}} aria-label={'切换到第 ' + (i + 1) + ' 段'} aria-pressed={index === i}/>)}</div>
        <p className="reader-credit">画面为 AI 改编。当前作品为短场景，不代表原作完整结局。</p>
      </div>
      <aside className={'reader-interact' + (paused ? ' is-paused' : '')}>
        <span className="reader-kicker">YOUR WHAT IF</span><h2>这一刻，你会怎么选？</h2>
        <p className="reader-pause-hint">{paused ? '故事停在这里。把你的想法留在这一秒。' : '随时暂停，不必等故事给你选项。'}</p>
        <textarea ref={input} aria-label="你的剧情想法" value={text} onChange={event => {setText(event.target.value); drafts.current[story.id] = event.target.value;}} placeholder="如果换我来演，这一刻我会……"/>
        <button className="reader-cta reader-save-wish" disabled={!paused || text.trim().length < 2 || busy} onClick={saveWish}>{busy ? '正在保存你的想法…' : '保存这一刻的想法'}</button>
        <div className="reader-feedback-slot"><InteractionFeedback title="故事回应" busy={busy} text={busy ? '正在记录暂停位置和你的剧情想法…' : reply} error={!busy && replyError}/></div>
        <small>会保存你的剧情想法，当前视频不会改变。自由改写后自动续播仍在开发中。</small>
        {wishes.map((wish, i) => <blockquote key={i}><small>{Math.floor(wish.at)} 秒 · 你的另一种可能</small><p>{wish.text}</p></blockquote>)}
      </aside></div>
    </section>}
    <footer className="reader-footer">叙间 · 让想象进入故事</footer>
  </div>;
}
