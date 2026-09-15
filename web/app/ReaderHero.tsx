'use client';

import Link from './ReaderLink';
import type {CSSProperties} from 'react';
import {ArrowDown, ArrowRight, Clapperboard, Sparkles} from 'lucide-react';
import ReaderArtwork from './ReaderArtwork';
import ReaderCreatorAvatar from './ReaderCreatorAvatar';
import {canWatch, productionLink, reducedMotion} from './reader-types';
import {circularOffset} from './reader-carousel';
import type {CatalogItem} from './reader-types';

type Props = {
  items: CatalogItem[];
  loading: boolean;
  activeId: string;
  onActiveChange: (id: string) => void;
  onOpen: (item: CatalogItem, focusKey: string) => void;
};

export default function ReaderHero({items, loading, activeId, onActiveChange, onOpen}: Props) {
  const current = Math.max(0, items.findIndex(item => item.id === activeId));
  const selected = items[current];

  function activate(id: string) {
    if (id !== activeId) onActiveChange(id);
  }

  return <section className="reader-hero catalog-hero reader-browse-hero" aria-labelledby="reader-welcome">
    <div className="reader-hero-copy">
      <div className="reader-kicker">知乎脑洞 · AI 漫剧</div>
      <h1 id="reader-welcome">这段脑洞，<br/>换你会怎么演？</h1>
      <p>看别人的版本，随时改写一刻；也可以从同一篇原作开始，完成属于你的漫剧。</p>
      {selected && <div className="reader-hero-paths">
        {canWatch(selected) && <button onClick={() => onOpen(selected, 'hero:' + selected.id)}>
          <Clapperboard size={15}/> 观看并临时改写
        </button>}
        {(selected.work_id || selected.project_id) && <Link href={productionLink(selected)} prefetch={false}>
          <Sparkles size={14}/> {selected.project_id ? '继续我的版本' : canWatch(selected) ? '制作我的完整版本' : '制作第一版'}
        </Link>}
      </div>}
      <small className="reader-hero-boundary">临时改写不进入个人作品；完整制作会在登录后归入你的账号。</small>
      <a href="#reader-shelf" className="reader-browse-all" onClick={event => {
        event.preventDefault();
        document.getElementById('reader-shelf')?.scrollIntoView({behavior: reducedMotion() ? 'instant' : 'smooth', block: 'start'});
      }}>进入故事市场 <ArrowDown size={15}/></a>
    </div>
    <div className="reader-cover-stage">
      {loading && !items.length ? <div className="reader-cover-skeleton" aria-label="正在准备故事封面" role="status">
        <i/><i/><i/><span className="feedback-sr-only">正在准备故事封面</span>
      </div> : items.length ? <ol className="reader-cover-deck" aria-label="循环翻看故事封面" aria-roledescription="轮播图" data-count={items.length}>
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
            onMouseEnter={() => activate(item.id)}
            style={{'--cover-x': `${desktopX}px`, '--cover-mobile-x': `${mobileX}px`, '--cover-scale': Math.max(.72, 1 - distance * .12),
              '--cover-y': `${distance * 11}px`, '--cover-rotate': `${direction * -7}deg`, '--cover-opacity': visible ? Math.max(.3, 1 - distance * .3) : 0,
              zIndex: 40 - distance * 5} as CSSProperties}>
            <Link href={playable ? '/?story=' + encodeURIComponent(item.release!.id) : productionLink(item)} prefetch={false}
              className="reader-cover-link" data-reader-focus={focusKey} draggable={false} tabIndex={visible ? 0 : -1}
              aria-label={(playable ? '观看并临时改写：' : '制作自己的版本：') + (item.title || '未提供标题')}
              aria-current={offset === 0 ? 'true' : undefined}
              onFocus={() => activate(item.id)}
              onClick={event => {
                if (offset !== 0) {
                  event.preventDefault();
                  activate(item.id);
                  return;
                }
                if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && playable) {
                  event.preventDefault();
                  onOpen(item, focusKey);
                }
              }}>
              <div className="reader-cover-visual">
                <ReaderArtwork src={visible ? artwork : undefined} order={index} eager={distance <= 1} priority={offset === 0} showNumber={false}/>
                {playable && <ReaderCreatorAvatar creator={item.release?.creator} className="reader-cover-creator"/>}
                <span className="reader-cover-status">{playable ? '公开放映' : '等待创作'}</span>
                <div className="reader-cover-copy">
                  <span className="reader-cover-tags">{item.labels.slice(0, 2).join(' · ') || '脑洞故事'}</span>
                  <h2>{item.title || '未提供标题'}</h2>
                  <span className="reader-cover-action">{playable ? '观看并改写' : '制作第一版'} <ArrowRight size={15}/></span>
                </div>
              </div>
            </Link>
          </li>;
        })}
      </ol> : <div className="reader-cover-empty"><span>故事封面还没到。</span><p>可以在下方重试读取目录。</p></div>}
    </div>
  </section>;
}
