'use client';

import Link from './ReaderLink';
import {useEffect, useState} from 'react';
import type {CSSProperties} from 'react';
import {ArrowDown, ArrowRight} from 'lucide-react';
import ReaderArtwork from './ReaderArtwork';
import {canWatch, productionLink, reducedMotion} from './reader-types';
import {circularOffset, wrapIndex} from './reader-carousel';
import type {CatalogItem} from './reader-types';

type Props = {
  items: CatalogItem[];
  loading: boolean;
  activeId: string;
  onActiveChange: (id: string) => void;
  onOpen: (item: CatalogItem, focusKey: string) => void;
};

export default function ReaderHero({items, loading, activeId, onActiveChange, onOpen}: Props) {
  const [pointerInside, setPointerInside] = useState(false);
  const [focusInside, setFocusInside] = useState(false);
  const [pageVisible, setPageVisible] = useState(true);
  const current = Math.max(0, items.findIndex(item => item.id === activeId));
  const nextId = items.length > 1 ? items[wrapIndex(current + 1, items.length)].id : '';
  const autoPaused = pointerInside || focusInside || !pageVisible;

  function activate(id: string) {
    if (id !== activeId) onActiveChange(id);
  }

  useEffect(() => {
    if (autoPaused || !nextId || reducedMotion()) return;
    const timer = window.setTimeout(() => {
      onActiveChange(nextId);
    }, 4200);
    return () => window.clearTimeout(timer);
  }, [autoPaused, nextId, onActiveChange]);

  useEffect(() => {
    const updateVisibility = () => setPageVisible(document.visibilityState === 'visible');
    updateVisibility();
    document.addEventListener('visibilitychange', updateVisibility);
    return () => document.removeEventListener('visibilitychange', updateVisibility);
  }, []);

  return <section className="reader-hero catalog-hero reader-browse-hero" aria-labelledby="reader-welcome">
    <div className="reader-hero-copy">
      <div className="reader-kicker">知乎脑洞 · AI 漫剧</div>
      <h1 id="reader-welcome">这段脑洞，<br/>换你会怎么演？</h1>
      <p>点击两侧卡片，翻到喜欢的，点开看看。</p>
      <a href="#reader-shelf" className="reader-browse-all" onClick={event => {
        event.preventDefault();
        document.getElementById('reader-shelf')?.scrollIntoView({behavior: reducedMotion() ? 'instant' : 'smooth', block: 'start'});
      }}>搜索或筛选故事 <ArrowDown size={15}/></a>
    </div>
    <div className="reader-cover-stage">
      {loading && !items.length ? <div className="reader-cover-skeleton" aria-label="正在准备故事封面" role="status">
        <i/><i/><i/><span className="feedback-sr-only">正在准备故事封面</span>
      </div> : items.length ? <>
        <ol className="reader-cover-deck" aria-label="循环翻看故事封面" aria-roledescription="轮播图" data-count={items.length}
          onMouseEnter={() => setPointerInside(true)} onMouseLeave={() => setPointerInside(false)}
          onFocusCapture={() => setFocusInside(true)} onBlurCapture={event => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocusInside(false);
          }}>
          {items.map((item, index) => {
            const playable = canWatch(item);
            const focusKey = 'hero:' + item.id;
            const offset = circularOffset(index, current, items.length);
            const distance = Math.abs(offset), direction = Math.sign(offset);
            const visible = distance <= 2;
            const desktopX = direction * (distance === 1 ? 154 : distance === 2 ? 258 : distance ? 340 : 0);
            const mobileX = direction * (distance === 1 ? 92 : distance === 2 ? 154 : distance ? 220 : 0);
            const artwork = item.artwork || item.tab_artwork;
            return <li key={item.id} className={'reader-cover-slot ' + (playable ? 'is-ready' : 'is-pending') + (offset === 0 ? ' is-active' : '') + (!visible ? ' is-distant' : '')}
              data-offset={offset} aria-hidden={!visible || undefined}
              aria-label={`${offset === 0 ? '当前作品，' : ''}第 ${index + 1} 张，共 ${items.length} 张`}
              style={{'--cover-x': `${desktopX}px`, '--cover-mobile-x': `${mobileX}px`, '--cover-scale': Math.max(.72, 1 - distance * .12),
                '--cover-y': `${distance * 11}px`, '--cover-rotate': `${direction * -7}deg`, '--cover-opacity': visible ? Math.max(.3, 1 - distance * .3) : 0,
                zIndex: 40 - distance * 5} as CSSProperties}>
              <Link href={playable ? '/?story=' + encodeURIComponent(item.release!.id) : productionLink(item)} prefetch={false}
                className="reader-cover-link" data-reader-focus={focusKey} draggable={false} tabIndex={visible ? 0 : -1}
                aria-label={(playable ? '观看漫剧：' : '原文与制作：') + (item.title || '未提供标题') + (playable ? '' : '（待生成漫剧）')}
                aria-current={offset === 0 ? 'true' : undefined}
                onClick={event => {
                  if (offset !== 0) {event.preventDefault(); activate(item.id); return;}
                  if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
                    if (playable) event.preventDefault();
                    onOpen(item, focusKey);
                  }
                }}>
                <div className="reader-cover-visual">
                  <ReaderArtwork src={visible ? artwork : undefined} order={index} eager={distance <= 1} priority={offset === 0} showNumber={false}/>
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
      </> : <div className="reader-cover-empty"><span>故事封面还没到。</span><p>可以在下方重试读取目录。</p></div>}
    </div>
  </section>;
}
