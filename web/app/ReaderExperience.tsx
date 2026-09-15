'use client';

import Link from './ReaderLink';
import {useEffect, useRef, useState} from 'react';
import {ArrowRight, Clapperboard, LoaderCircle, Search, Sparkles} from 'lucide-react';
import {InteractionFeedback, workStages} from './ProgressFeedback';
import ReaderArtwork from './ReaderArtwork';
import ReaderCreatorAvatar from './ReaderCreatorAvatar';
import ReaderHero from './ReaderHero';
import ReaderStoryGrid from './ReaderStoryGrid';
import {canWatch, groupProductionsByStory, productionLink} from './reader-types';
import {modelAccessHeaders} from './model-access';
import AccountDock from './AccountDock';
import {rankByProduction} from './reader-carousel';
import type {Catalog, CatalogItem, ReaderBranch, Release, ReleaseEntry} from './reader-types';
import './reader-catalog.css';
import './reader-motion.css';

type Wish = {at: number; text: string};
type BrowseState = {query: string; filter: string; carouselIds: string[]; activeId: string; scrollY: number; focusKey: string};
const browseKey = 'storyloom.reader.browse.v1';

export default function ReaderExperience() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [query, setQuery] = useState(''), [searchInput, setSearchInput] = useState('');
  const [filter, setFilter] = useState('ready'), [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true), [carouselIds, setCarouselIds] = useState<string[]>([]);
  const [activeId, setActiveId] = useState('');
  const [story, setStory] = useState<Release | null>(null), [index, setIndex] = useState(0);
  const [playbackEntries, setPlaybackEntries] = useState<ReleaseEntry[]>([]);
  const [branch, setBranch] = useState<ReaderBranch | null>(null), [manifestBranchId, setManifestBranchId] = useState('');
  const [paused, setPaused] = useState(true), [moment, setMoment] = useState(0);
  const [text, setText] = useState(''), [wishes, setWishes] = useState<Wish[]>([]);
  const [reply, setReply] = useState(''), [replyError, setReplyError] = useState(false);
  const [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  const [seekNotice, setSeekNotice] = useState('');
  // Which production of a story is on top of its stack. Keyed by story, defaulting to the newest.
  const [versionChoice, setVersionChoice] = useState<Record<string, number>>({});
  const video = useRef<HTMLVideoElement>(null), input = useRef<HTMLTextAreaElement>(null);
  const continuePlay = useRef(false), openedLink = useRef(false), composing = useRef(false);
  const catalogRef = useRef<Catalog | null>(null), currentStory = useRef<string | null>(null);
  const requestVersion = useRef(0);
  const readerSession = useRef(''), manifestBranch = useRef(''), playbackIndex = useRef(0);
  const safeTime = useRef(0), internalSeek = useRef(false), waitingForNext = useRef(false), finishedEntry = useRef('');
  const resumeAt = useRef<number | null>(null);
  const watchedUntil = useRef<Record<string, number>>({});
  const restore = useRef(false), savedBrowse = useRef<BrowseState | null>(null);
  const carouselInitialized = useRef(false);
  const browse = useRef({query, filter, carouselIds, activeId});
  browse.current = {query, filter, carouselIds, activeId};
  playbackIndex.current = index;
  const entry = playbackEntries[index];
  const nextEntry = playbackEntries[index + 1];
  const forwardLocked = !!manifestBranchId || (!!branch?.lock_forward_seek && branch.status !== 'rejected');
  const branchWorking = branch?.status === 'planning' || branch?.status === 'generating';
  const awaitingFirstBranch = !!branchWorking && manifestBranchId !== branch?.id;
  const blockedAtCut = busy || (!!branch?.lock_forward_seek && branch.status !== 'rejected' && manifestBranchId !== branch.id);

  function sessionToken() {
    if (readerSession.current) return readerSession.current;
    // A branch is deliberately temporary. Its opaque token lives only in this mounted page, so a
    // reload or leaving the screening room cannot silently restore it as a saved personal work.
    return readerSession.current = crypto.randomUUID();
  }

  function remember(focusKey = '', selectedId?: string) {
    const state = {...browse.current, activeId: selectedId || browse.current.activeId, scrollY: window.scrollY, focusKey};
    savedBrowse.current = state;
    try {sessionStorage.setItem(browseKey, JSON.stringify(state));} catch {}
  }

  function showStory(release: Release) {
    video.current?.pause();
    readerSession.current = '';
    currentStory.current = release.id;
    requestVersion.current++;
    continuePlay.current = false;
    manifestBranch.current = ''; waitingForNext.current = false;
    resumeAt.current = null;
    setStory(release); setPlaybackEntries(release.entries); setIndex(0); setMoment(release.entries[0].start); setPaused(true);
    setBranch(null); setManifestBranchId(''); setSeekNotice(''); safeTime.current = release.entries[0].start;
    setText(''); setWishes([]);
    setReply(''); setReplyError(false); setMessage(''); setBusy(false);
    window.scrollTo({top: 0, behavior: 'instant'});
  }

  function returnToCatalog() {
    video.current?.pause();
    readerSession.current = '';
    currentStory.current = null;
    requestVersion.current++;
    continuePlay.current = false;
    restore.current = true;
    manifestBranch.current = ''; waitingForNext.current = false;
    resumeAt.current = null;
    setStory(null); setPlaybackEntries([]); setBranch(null); setManifestBranchId(''); setBusy(false);
    setText(''); setWishes([]); setReply(''); setReplyError(false); setSeekNotice('');
  }

  function openItem(item: CatalogItem, focusKey: string) {
    remember(focusKey, focusKey.startsWith('hero:') ? item.id : undefined);
    if (!canWatch(item)) return;
    openRelease(item, item.release!, focusKey);
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

  /** Open one specific production, so a story made by several people can be watched one at a time. */
  function openRelease(item: CatalogItem, release: Release, focusKey: string) {
    if (!release.entries.length) return;
    remember(focusKey);
    const url = new URL(window.location.href);
    url.searchParams.set('story', release.id); url.hash = '';
    window.history.pushState({...window.history.state, readerWatch: true}, '', url);
    showStory(release);
  }

  useEffect(() => {
    try {
      const state = JSON.parse(sessionStorage.getItem(browseKey) || 'null');
      if (state && typeof state.query === 'string') {
        const restored: BrowseState = {
          query: state.query,
          filter: ['all', 'ready', 'pending'].includes(state.filter) ? state.filter : 'ready',
          carouselIds: (Array.isArray(state.carouselIds) ? state.carouselIds : Array.isArray(state.featuredIds) ? state.featuredIds : [])
            .filter((id: unknown) => typeof id === 'string'),
          activeId: typeof state.activeId === 'string' ? state.activeId : '',
          scrollY: Number.isFinite(state.scrollY) ? Math.max(0, state.scrollY) : 0,
          focusKey: typeof state.focusKey === 'string' ? state.focusKey : '',
        };
        savedBrowse.current = restored; restore.current = true;
        setQuery(restored.query); setSearchInput(restored.query); setFilter(restored.filter);
        setCarouselIds(restored.carouselIds); setActiveId(restored.activeId);
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
        const response = await fetch('/api/reader/catalog' + (force ? '?refresh=true' : ''), {
          signal: abort.signal, headers: modelAccessHeaders(),
        });
        if (!response.ok) throw Error('暂时无法读取故事市场，请稍后重试。');
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
    const ranked = rankByProduction(catalog.items);
    if (!carouselInitialized.current) {
      carouselInitialized.current = true;
      setCarouselIds(previous => previous.length ? [...new Set([...previous.filter(id => available.has(id)), ...ranked.map(item => item.id)])] : ranked.map(item => item.id));
      setActiveId(previous => available.has(previous) ? previous : ranked[0]?.id || '');
      return;
    }
    setCarouselIds(previous => {
      const retained = previous.filter(id => available.has(id));
      const next = [...new Set([...retained, ...ranked.map(item => item.id)])];
      return next.join('|') === previous.join('|') ? previous : next;
    });
  }, [catalog]);

  useEffect(() => {
    if (!carouselIds.includes(activeId)) setActiveId(carouselIds[0] || '');
  }, [carouselIds, activeId]);

  useEffect(() => {
    if (loading || story || !restore.current || !catalog || (catalog.items.length && !carouselIds.length)) return;
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
  }, [loading, story, catalog, carouselIds]);

  function pause(focus = true) {
    video.current?.pause(); setPaused(true);
    setMoment(video.current?.currentTime ?? entry?.start ?? 0);
    if (focus) input.current?.focus({preventScroll: true});
  }

  function entryKey(item: ReleaseEntry | undefined, position = index) {
    return item?.occurrence_id || (story ? story.id + ':original:' + position : 'none');
  }

  function activateBranch(next: ReaderBranch) {
    if (next.entries[0]?.status !== 'ready') return;
    if (manifestBranch.current !== next.id) {
      video.current?.pause();
      manifestBranch.current = next.id; waitingForNext.current = false;
      setManifestBranchId(next.id); setPlaybackEntries(next.entries); setIndex(0);
      const start = next.entries[0].start || 0;
      safeTime.current = start; watchedUntil.current[entryKey(next.entries[0], 0)] = start;
      setMoment(start); setPaused(true); continuePlay.current = true;
      setReply('第一段分支已就绪，正在衔接播放；后续片段继续在后台准备。');
      return;
    }
    setPlaybackEntries(next.entries);
    const currentIndex = playbackIndex.current;
    if (waitingForNext.current && next.entries[currentIndex + 1]?.status === 'ready') {
      waitingForNext.current = false; continuePlay.current = true;
      const start = next.entries[currentIndex + 1].start || 0;
      safeTime.current = start; setMoment(start); setIndex(currentIndex + 1);
    }
  }

  useEffect(() => {
    if (!branch?.id || ['ready', 'rejected', 'failed', 'superseded'].includes(branch.status)) return;
    let alive = true, timer: ReturnType<typeof setTimeout>;
    async function pollBranch() {
      try {
        const response = await fetch('/api/reader/branches/' + encodeURIComponent(branch!.id), {
          headers: {'X-Reader-Session': sessionToken()}, cache: 'no-store',
        });
        const data: ReaderBranch & {detail?: string} = await response.json();
        if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '读取分支进度失败。');
        if (!alive) return;
        setBranch(data); setReply(data.message); setReplyError(data.status === 'failed' || data.status === 'rejected');
        activateBranch(data);
        if (!['ready', 'rejected', 'failed', 'superseded'].includes(data.status)) {
          timer = setTimeout(pollBranch, data.ready_count ? 1200 : 1800);
        }
      } catch (error) {
        if (alive) {setReply((error as Error).message); setReplyError(true); timer = setTimeout(pollBranch, 3000);}
      }
    }
    timer = setTimeout(pollBranch, 700);
    return () => {alive = false; clearTimeout(timer);};
  }, [branch?.id]);

  const elapsed = story ? playbackEntries.slice(0, index).reduce((total, item) =>
    total + Math.max(0, (item.end || 0) - (item.start || 0)), 0) + Math.max(0, moment - (entry?.start || 0)) : 0;

  async function saveWish() {
    if (!story || !entry || busy || awaitingFirstBranch || !paused || text.trim().length < 2) return;
    const submittedText = text.trim(), releaseId = story.id, at = video.current?.currentTime ?? entry.start;
    const submittedElapsed = elapsed, version = ++requestVersion.current;
    setBusy(true); setReply(''); setReplyError(false);
    try {
      const response = await fetch('/api/reader/branches', {method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-Reader-Session': sessionToken(), ...modelAccessHeaders(),
      }, body: JSON.stringify({
        release_id: releaseId, base_branch_id: manifestBranch.current || undefined,
        index, offset: Math.max(entry.start, Math.min(entry.end, at)), text: submittedText, confirm_generation: true,
      })});
      const data: ReaderBranch & {detail?: string} = await response.json();
      if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '保存失败，请重试。');
      if (currentStory.current === releaseId && requestVersion.current === version) {
        setWishes(current => [...current, {at: submittedElapsed, text: submittedText}]);
        setText(current => current.trim() === submittedText ? '' : current);
        setBranch(data); setReply(data.message); setReplyError(false);
      }
    } catch (error) {
      if (currentStory.current === releaseId && requestVersion.current === version) {setReply((error as Error).message); setReplyError(true);}
    } finally {
      if (currentStory.current === releaseId && requestVersion.current === version) setBusy(false);
    }
  }

  function loadedEntry() {
    const current = video.current;
    if (!current || !entry || entry.status === 'pending' || entry.status === 'failed') return;
    const key = entryKey(entry);
    const start = resumeAt.current ?? entry.start ?? 0;
    resumeAt.current = null;
    internalSeek.current = false; finishedEntry.current = ''; safeTime.current = watchedUntil.current[key] || start;
    current.currentTime = start;
    if (continuePlay.current) {
      continuePlay.current = false;
      current.play().catch(() => setPaused(true));
    }
  }

  function preventForwardSeek() {
    const current = video.current;
    if (!current || !forwardLocked || internalSeek.current) return;
    if (current.currentTime > safeTime.current + .08) {
      internalSeek.current = true; current.currentTime = safeTime.current;
      setSeekNotice('分支生成后不能向前跳看；播放到达后会自动解锁相应进度。');
    }
  }

  function startPlayback() {
    if (blockedAtCut) {
      video.current?.pause(); setPaused(true);
      setReply(branch?.status === 'failed' ? '分支尚未完成，请在当前画面重新提交一种可生成的选择。' : '请稍等第一段分支，原版后续不会越过你的选择继续播放。');
      return;
    }
    setPaused(false);
  }

  function finishEntry() {
    const finished = entryKey(entry);
    if (finishedEntry.current === finished) return;
    finishedEntry.current = finished;
    const following = playbackEntries[index + 1];
    if (following?.status === 'ready' || (following && !following.status)) {
      continuePlay.current = true; waitingForNext.current = false;
      safeTime.current = following.start || 0; setIndex(index + 1); setMoment(following.start || 0);
    } else if (following?.status === 'pending') {
      waitingForNext.current = true; setPaused(true);
      setReply('下一段还在后台生成，准备好后会自动接着播放。'); setReplyError(false);
    } else if (following?.status === 'failed') {
      waitingForNext.current = false; setPaused(true);
      setReply(following.message || '下一段生成失败，当前画面和已完成分支均已保留。'); setReplyError(true);
    } else {
      waitingForNext.current = false; setPaused(true);
      if (manifestBranch.current && branch?.terminal) {
        setReply('这次改写已在因果完整处收束，没有强行回到原结局。'); setReplyError(false);
      }
    }
  }

  function updatePlaybackTime() {
    const current = video.current;
    if (!current || !entry) return;
    setMoment(current.currentTime);
    if (!current.seeking && !internalSeek.current) {
      const key = entryKey(entry);
      safeTime.current = Math.max(safeTime.current, current.currentTime);
      watchedUntil.current[key] = Math.max(watchedUntil.current[key] || entry.start || 0, current.currentTime);
    }
    if (current.currentTime >= entry.end - .04 && !current.paused) {
      current.pause(); finishEntry();
    }
  }

  function chooseEntry(target: number) {
    const selected = playbackEntries[target];
    if (!selected || selected.status === 'pending' || selected.status === 'failed') return;
    if (forwardLocked && target > index) {
      setSeekNotice('分支生成后不能向前跳看，请按顺序观看。'); return;
    }
    pause(false); continuePlay.current = false; waitingForNext.current = false;
    setIndex(target); setMoment(selected.start || 0);
  }

  const visible = (catalog?.items || []).filter(item =>
    (filter === 'ready' ? canWatch(item) : filter === 'pending' ? !canWatch(item) : true) &&
    ((item.title || '') + ' ' + (item.description || '')).toLowerCase().includes(query.toLowerCase()));
  const carouselItems = carouselIds.map(id => catalog?.items.find(item => item.id === id)).filter((item): item is CatalogItem => !!item);
  const shelfGroups = groupProductionsByStory(visible, catalog?.releases || []).map((group, order) => ({...group, order}));
  const productionTotal = (catalog?.releases || []).length;
  const clearFilters = () => {setFilter('all'); setQuery(''); setSearchInput('');};

  return <div className="reader-world">
    <nav className="reader-nav">
      <button className="reader-brand" onClick={() => {if (story) back(); else window.scrollTo({top: 0, behavior: 'instant'});}}>叙间<span>每个故事，都有另一种可能</span></button>
      <div className="reader-entry-links">
        <Link className="reader-work-link" href="/author" prefetch={false} onClick={() => {if (!story) remember('nav:author');}} data-reader-focus="nav:author">我的制作 <ArrowRight size={14}/></Link>
        <AccountDock next={story ? '/?story=' + encodeURIComponent(story.id) : '/'}/>
      </div>
    </nav>
    {!story ? <div className="reader-catalog-page">
      <ReaderHero items={carouselItems} loading={loading || (!!catalog?.items.length && !carouselItems.length)} activeId={activeId} onActiveChange={setActiveId} onOpen={openItem}/>
      <section id="reader-shelf" className="reader-shelf">
        <div className="reader-section-title catalog-market-head">
          <div className="catalog-market-copy"><span className="reader-kicker">STORY MARKET</span><h2>故事市场</h2>
            <p>观看公开版本并临时改写，或从同一篇原作开始，制作你的完整漫剧。</p>
          </div>
          {catalog ? <div className="catalog-summary" aria-label={`${catalog.brainstorm_count} 篇原作，${productionTotal} 个公开版本`}>
            <span><strong>{catalog.brainstorm_count}</strong> 篇原作</span><i/><span><strong>{productionTotal}</strong> 个公开版本</span>
          </div> : <div className="catalog-summary is-loading">正在载入市场…</div>}
        </div>
        <div className="catalog-toolbar">
          <div className="catalog-filters" aria-label="筛选故事市场">{[['ready', '正在放映'], ['pending', '等待创作'], ['all', '全部原作']].map(([value, label]) =>
            <button key={value} aria-pressed={filter === value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div>
          <label className="catalog-search"><Search size={16}/><input aria-label="搜索原作" placeholder="搜索标题或简介" value={searchInput}
            onCompositionStart={() => {composing.current = true;}}
            onCompositionEnd={event => {composing.current = false; setQuery(event.currentTarget.value);}}
            onChange={event => {setSearchInput(event.target.value); if (!composing.current) setQuery(event.target.value);}}/></label>
          <button className="catalog-refresh" disabled={loading} onClick={() => setRefresh(value => value + 1)}>
            {loading && <LoaderCircle size={13} className="feedback-spin"/>}{loading ? '刷新中…' : '刷新'}
          </button>
        </div>
        {catalog?.warning && <div className="catalog-warning" role="status">{catalog.stale ? '故事服务暂不可用，正在显示已保存的市场内容。' : '故事市场暂时无法读取，当前仅展示本地可观看的作品。'}{catalog.fetched_at && <small>内容保存于 {new Date(catalog.fetched_at * 1000).toLocaleString('zh-CN')}</small>}</div>}
        {message && <div className="catalog-warning catalog-error" role="alert">{message}<button onClick={() => setRefresh(value => value + 1)} disabled={loading}>重试读取</button></div>}
        {loading && !catalog ? <div className="reader-story-grid catalog-skeleton-grid" role="status" aria-label="正在打开故事市场"><span className="feedback-sr-only">正在打开故事市场</span>{[0, 1, 2].map(id =>
          <div className="reader-story-card catalog-skeleton" key={id} aria-hidden="true"><div className="catalog-poster"/><div className="catalog-story-info"><i/><i/><i/></div></div>)}</div>
          : !visible.length ? <div className="reader-empty" role="status">
            <span>{query ? '没有找到匹配的原作。' : filter === 'ready' ? '还没有公开放映的版本。' : '暂时没有可展示的原作。'}</span>
            <p>{filter === 'ready' && !query ? '切到「等待创作」，从一篇原作制作第一个完整版本。' : '可以刷新内容或调整筛选条件。'}</p>
            {(filter !== 'all' || query) && <button onClick={clearFilters}>浏览全部原作 →</button>}
          </div> : <ReaderStoryGrid ids={shelfGroups.map(group => group.item.id)}>{shelfGroups.map(({item, productions, stacked, order}) => {
            const active = Math.min(versionChoice[item.id] ?? 0, Math.max(0, productions.length - 1));
            const release = productions[active] || item.release;
            const playable = !!release && release.entries.length > 0;
            const cover = item.artwork || item.tab_artwork;
            const stage = workStages.find(candidate => candidate.id === item.stage);
            const posterKey = 'shelf:poster:' + item.id, sourceKey = 'shelf:source:' + item.id, watchKey = 'shelf:watch:' + item.id;
            return <article className={'reader-story-card ' + (playable ? 'is-ready' : 'is-pending') + (stacked ? ' has-stack' : '')}
                            key={item.id} data-story-id={item.id} data-productions={productions.length}>
              {stacked && <span className="catalog-stack" aria-hidden="true"><i/><i/></span>}
              <div className="catalog-poster">
                {playable ? <button aria-label={'观看并临时改写《' + item.title + '》'} data-reader-focus={posterKey} onClick={() => openRelease(item, release!, posterKey)}>
                  {cover ? <ReaderArtwork src={cover} order={order}/> : <video src={release!.entries[0].media} preload="none" muted/>}
                  <span className="catalog-play"><Clapperboard size={22}/> 观看并改写</span>
                </button> : <Link href={productionLink(item)} prefetch={false} aria-label={'制作《' + item.title + '》的第一个漫剧版本'} data-reader-focus={posterKey} onClick={() => remember(posterKey)}>
                  <ReaderArtwork src={cover} order={order}/><span className="catalog-not-made">还没有人做出漫剧</span>
                </Link>}
                {playable && <ReaderCreatorAvatar creator={release?.creator} className="catalog-creator"/>}
                <span className="catalog-status">{playable ? '公开放映 · 可临时改写' : stage ? '我的制作 · ' + stage.name : '等待创作'}</span>
                {stacked && <span className="catalog-stack-count">{productions.length} 个版本</span>}
              </div>
              <div className="catalog-story-info">
                <div className="catalog-tags">{item.labels.slice(0, 3).map(label => <span key={label}>{label}</span>)}</div>
                <h3>{item.title || '未提供标题'}</h3><p>{item.description || '打开微小说详情，阅读原文并选择制作风格。'}</p>
                {stacked && <div className="catalog-versions" role="group" aria-label={'《' + (item.title || '') + '》的 ' + productions.length + ' 个版本'}>
                  <small>同一篇原作，{productions.length} 个互相独立的完整版本</small>
                  <div className="catalog-version-list">{productions.map((production, index) =>
                    <button key={production.id} aria-pressed={index === active} className={index === active ? 'selected' : ''}
                            title={production.description || production.title || ''}
                            onClick={() => setVersionChoice(current => ({...current, [item.id]: index}))}>
                      <span>{production.mine ? '我的版本' : production.title || '版本 ' + (index + 1)}</span>
                      {production.mine ? <em>我的</em> : index === 0 && <em>最新</em>}
                    </button>)}</div>
                </div>}
                {playable && <p className="catalog-mode-note">临时改写只在本次观看中生效，不会存入个人作品。</p>}
                <div className="catalog-card-actions">{playable ? <>
                  <button onClick={() => openRelease(item, release!, watchKey)} data-reader-focus={watchKey}>观看并改写 <ArrowRight size={15}/></button>
                  {(item.work_id || item.project_id) && <Link className="catalog-create" href={productionLink(item)} prefetch={false} data-reader-focus={sourceKey} onClick={() => remember(sourceKey)}>
                    <Sparkles size={13}/>{item.project_id ? '继续我的完整版本' : '制作我的完整版本'}
                  </Link>}
                </> : <Link href={productionLink(item)} prefetch={false} data-reader-focus={sourceKey} onClick={() => remember(sourceKey)}>
                  <Sparkles size={14}/>{item.project_id ? '继续我的版本' : '制作第一版'} <ArrowRight size={15}/>
                </Link>}</div>
              </div>
            </article>;
          })}</ReaderStoryGrid>}
        {catalog?.catalog_available && <p className="catalog-footnote">观看入口打开别人已经发布的版本，改写不会被保存或公开；制作入口始终按当前登录账号创建或继续一个独立项目，不会进入发布者的项目。</p>}
      </section>
    </div> : <section className="reader-screen">
      <button className="reader-back" onClick={back}>← 返回故事</button>
      <header><small>改编自《{story.source_title}》 · {story.author || '作者未提供'}</small><h1>{story.title}</h1>
        <div className="reader-source-actions"><span>临时改写 · 不进入个人作品</span>
          {(story.source_work_id || story.project_id) && <Link className="reader-source-link" href={productionLink(story)} prefetch={false}><Sparkles size={13}/> 基于同一原作，制作我的完整版本 <ArrowRight size={14}/></Link>}
        </div>
      </header>
      <div className="reader-view-grid"><div>
        <div className="reader-cinema"><video key={story.id + ':' + manifestBranchId + ':' + entryKey(entry)} ref={video}
          src={entry && entry.status !== 'pending' && entry.status !== 'failed' ? entry.media : undefined} controls playsInline preload="auto"
          onLoadedMetadata={loadedEntry} onSeeking={preventForwardSeek} onSeeked={() => {internalSeek.current = false;}}
          onPlay={startPlayback} onPause={() => {setPaused(true); setMoment(video.current?.currentTime ?? 0);}}
          onTimeUpdate={updatePlaybackTime} onEnded={finishEntry}/>
          {nextEntry?.status === 'ready' && nextEntry.media && <video className="reader-video-preload" src={nextEntry.media} preload="auto" muted playsInline aria-hidden="true"/>}
        </div>
        {branch && <div className={'reader-branch-status is-' + branch.status} role="status" aria-live="polite">
          <span>{branchWorking && <LoaderCircle size={13} className="feedback-spin"/>}<strong>你的分支 · v{branch.branch_version}</strong></span>
          <p>{branch.message}</p>{branch.generated_count > 0 && <small>{branch.ready_count} / {branch.generated_count} 段已就绪 · 仅复用当前场景、人物和着装</small>}
        </div>}
        {seekNotice && <p className="reader-seek-notice" role="status">{seekNotice}</p>}
        <div className="reader-playbar"><span>{entry?.kind === 'branch' ? '分支镜头' : '第'} {index + 1} / {playbackEntries.length} 段 · {Math.floor(elapsed)} 秒</span><button onClick={() => pause()}>暂停，改写这一刻</button></div>
        <div className="reader-segments">{playbackEntries.map((segment, i) => <button key={segment.occurrence_id || segment.clip_id + ':' + i}
          className={(index === i ? 'active ' : '') + (segment.status === 'pending' ? 'pending ' : '') + (segment.kind === 'branch' ? 'branch' : '')}
          disabled={segment.status === 'pending' || segment.status === 'failed' || (forwardLocked && i > index)}
          onClick={() => chooseEntry(i)} aria-label={(segment.label || '第 ' + (i + 1) + ' 段') + (segment.status === 'pending' ? '，生成中' : '')} aria-pressed={index === i}/>)}</div>
        <p className="reader-credit">画面为 AI 改编。当前作品为短场景，不代表原作完整结局。</p>
      </div>
      <aside className={'reader-interact' + (paused ? ' is-paused' : '')}>
        <span className="reader-kicker">TEMPORARY REWRITE</span><h2>这一刻，你会怎么选？</h2>
        <p className="reader-pause-hint">{paused ? '故事停在这里。写下另一种选择，看看这一幕会怎样发生。' : '随时暂停，不必等故事给你选项。'}</p>
        <textarea ref={input} aria-label="你的剧情想法" value={text} onChange={event => setText(event.target.value)} placeholder="如果换我来演，这一刻我会……"/>
        <button className="reader-cta reader-save-wish" disabled={!paused || text.trim().length < 2 || busy || awaitingFirstBranch} onClick={saveWish}>{busy ? '正在提交这一刻…' : awaitingFirstBranch ? '正在准备第一段分支…' : '生成这一种可能'}</button>
        <div className="reader-feedback-slot"><InteractionFeedback title="故事回应" busy={busy || !!branchWorking} text={busy ? '正在冻结暂停画面与当前剧情状态…' : reply} error={!busy && replyError}/></div>
        {replyError && /模型|权限|配置|算力豆/.test(reply) && <span className="reader-model-link">请在右上角的账号面板里登录领取算力豆，或填写自己的 API Key <ArrowRight size={13}/></span>}
        <p className="reader-temporary-note">这次改写仅在当前观看页面生效；离开或刷新后不会恢复，也不会出现在「我的制作」或公开版本中。</p>
        <small>改写只复用当前场景、人物与着装。分支开始后不能向前跳看；后续视频会逐段生成并提前加载。</small>
        {wishes.map((wish, i) => <blockquote key={i}><small>{Math.floor(wish.at)} 秒 · 你的另一种可能</small><p>{wish.text}</p></blockquote>)}
      </aside></div>
    </section>}
    <footer className="reader-footer">叙间 · 让想象进入故事</footer>
  </div>;
}
