'use client';

import Link from './ReaderLink';
import {useEffect, useRef, useState} from 'react';
import type {CSSProperties} from 'react';
import {ArrowDown, ArrowRight, ChevronLeft, ChevronRight} from 'lucide-react';
import ReaderArtwork from './ReaderArtwork';
import {canWatch, productionLink, reducedMotion} from './reader-types';
import type {CatalogItem} from './reader-types';

type Props = {
  items: CatalogItem[];
  loading: boolean;
  activeId: string;
  onActiveChange: (id: string) => void;
  onOpen: (item: CatalogItem, focusKey: string) => void;
};

export default function ReaderHero({items, loading, activeId, onActiveChange, onOpen}: Props) {
  const deck = useRef<HTMLOListElement>(null);
  const scrollFrame = useRef(0);
  const wasRail = useRef(false);
  const navigated = useRef(true);
  const [canScroll, setCanScroll] = useState(false);
  const current = Math.max(0, items.findIndex(item => item.id === activeId));
  const currentRef = useRef(current);
  currentRef.current = current;

  function activate(id: string) {
    navigated.current = true;
    if (id !== activeId) onActiveChange(id);
  }

  useEffect(() => {
    const list = deck.current;
    if (!list) return;
    const measure = () => {
      const rail = getComputedStyle(list).display === 'flex';
      if (rail && !wasRail.current) {
        const selected = list.children[currentRef.current] as HTMLElement | undefined;
        if (selected) {navigated.current = true; list.scrollLeft = selected.offsetLeft;}
      }
      wasRail.current = rail;
      // A raised desktop cover may extend a few pixels beyond the deck; that is not a rail.
      setCanScroll(rail && list.scrollWidth > list.clientWidth + 2);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(list);
    measure();
    return () => {observer.disconnect(); wasRail.current = false; cancelAnimationFrame(scrollFrame.current); scrollFrame.current = 0;};
  }, [items.length]);

  function browse(direction: number) {
    if (!items.length) return;
    const next = Math.max(0, Math.min(items.length - 1, current + direction));
    const card = deck.current?.children[next] as HTMLElement | undefined;
    navigated.current = true;
    onActiveChange(items[next].id);
    if (card && deck.current) deck.current.scrollTo({left: card.offsetLeft, behavior: reducedMotion() ? 'instant' : 'smooth'});
  }

  function trackScroll() {
    if (!deck.current || !canScroll || navigated.current || scrollFrame.current) return;
    scrollFrame.current = requestAnimationFrame(() => {
      scrollFrame.current = 0;
      const list = deck.current;
      if (!list || navigated.current) return;
      const cards = Array.from(list.children) as HTMLElement[];
      // The last cover cannot always align to the left edge; compare centres instead.
      const center = list.scrollLeft + list.clientWidth / 2;
      const closest = cards.reduce((best, card, index) =>
        Math.abs(card.offsetLeft + card.offsetWidth / 2 - center) < Math.abs(cards[best].offsetLeft + cards[best].offsetWidth / 2 - center) ? index : best, 0);
      if (items[closest]) onActiveChange(items[closest].id);
    });
  }

  return <section className="reader-hero catalog-hero reader-browse-hero" aria-labelledby="reader-welcome">
    <div className="reader-hero-copy">
      <div className="reader-kicker">知乎脑洞 · AI 漫剧</div>
      <h1 id="reader-welcome">这段脑洞，<br/>换你会怎么演？</h1>
      <p>翻到喜欢的，点开看看。</p>
      <a href="#reader-shelf" className="reader-browse-all" onClick={event => {
        event.preventDefault();
        document.getElementById('reader-shelf')?.scrollIntoView({behavior: reducedMotion() ? 'instant' : 'smooth', block: 'start'});
      }}>看看全部故事 <ArrowDown size={15}/></a>
    </div>
    <div className="reader-cover-stage">
      {loading && !items.length ? <div className="reader-cover-skeleton" aria-label="正在准备故事封面" role="status">
        <i/><i/><i/><span className="feedback-sr-only">正在准备故事封面</span>
      </div> : items.length ? <>
        <ol className="reader-cover-deck" ref={deck} onScroll={trackScroll}
          onPointerDown={() => {navigated.current = false;}} onWheel={() => {navigated.current = false;}} onKeyDown={() => {navigated.current = false;}}
          aria-label="随手翻翻故事封面" data-count={items.length}>
          {items.map((item, index) => {
            const playable = canWatch(item);
            const focusKey = 'hero:' + item.id;
            return <li key={item.id} className={'reader-cover-slot ' + (playable ? 'is-ready' : 'is-pending') + (index === current ? ' is-active' : '')}
              style={{'--cover-position': index / Math.max(1, items.length - 1),
                zIndex: index === current ? 30 : 20 - Math.abs(index - current) * 2 + (index < current ? 1 : 0)} as CSSProperties}>
              <Link href={playable ? '/?story=' + encodeURIComponent(item.release!.id) : productionLink(item)} prefetch={false}
                className="reader-cover-link" data-reader-focus={focusKey} draggable={false}
                aria-label={(playable ? '观看漫剧：' : '原文与制作：') + (item.title || '未提供标题') + (playable ? '' : '（待生成漫剧）')}
                aria-current={index === current ? 'true' : undefined}
                onPointerEnter={event => {if (event.pointerType !== 'touch') activate(item.id);}}
                onFocus={() => activate(item.id)} onClick={event => {
                  if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
                    if (playable) event.preventDefault();
                    onOpen(item, focusKey);
                  }
                }}>
                <div className="reader-cover-visual">
                  <ReaderArtwork src={item.artwork || item.tab_artwork} order={index} eager/>
                  <span className="reader-cover-number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
                  <span className="reader-cover-status">{playable ? '已有漫剧' : '待生成漫剧'}</span>
                  <div className="reader-cover-copy">
                    <span className="reader-cover-tags">{item.labels.slice(0, 2).join(' · ') || '脑洞故事'}</span>
                    <h2>{item.title || '未提供标题'}</h2>
                    <span className="reader-cover-action">{playable ? '观看漫剧' : '原文与制作'} <ArrowRight size={15}/></span>
                  </div>
                </div>
              </Link>
            </li>;
          })}
        </ol>
        <div className="reader-cover-navigation" hidden={!canScroll} aria-label="翻看封面">
          <button type="button" onClick={() => browse(-1)} disabled={current === 0}><ChevronLeft size={15}/>上一张</button>
          <span aria-live="polite">{current + 1} / {items.length}</span>
          <button type="button" onClick={() => browse(1)} disabled={current === items.length - 1}>下一张<ChevronRight size={15}/></button>
        </div>
      </> : <div className="reader-cover-empty"><span>故事封面还没到。</span><p>可以在下方重试读取目录。</p></div>}
    </div>
  </section>;
}
