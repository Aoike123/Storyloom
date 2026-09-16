'use client';
import type {ReactNode} from 'react';

type Episode = {
  segment_id: string; title?: string; beat?: string; source_refs?: string[]; shot_budget?: number;
  characters?: string[]; location?: string; purpose?: string; continuity_out?: string;
  source_text?: string; storyboarded?: boolean; render_complete?: boolean; published?: boolean;
  shots?: number; clips?: number;
};

/**
 * The cut: the episode list this film is made of.
 *
 * On the review step this is the main content, with the author's confirmation block passed in as
 * children; on later steps it collapses into a one-line summary that still shows how far each
 * episode has come, which is what makes 发布 / 继续 an informed choice.
 */
function EpisodeList({episodes,approved}: {episodes: Episode[]; approved: boolean}) {
  return <ol className="segment-review-list">{episodes.map(episode => <li key={episode.segment_id}>
    <header>
      <strong>{episode.segment_id} · {episode.title || '未命名情节'}</strong>
      <span>{episode.beat || '剧情段落'} · {(episode.source_refs || []).join('、')} · {episode.shot_budget || 0} 镜</span>
    </header>
    <p>{episode.purpose}</p>
    <small>出场：{(episode.characters || []).join('、') || '未点名'} · 场景：{episode.location || '未指明'} · 结尾：{episode.continuity_out || '—'}</small>
    {episode.source_text && <blockquote>{episode.source_text}</blockquote>}
    {approved && <em className={'segment-state ' + (episode.published ? 'is-published' : episode.render_complete ? 'is-ready' : episode.storyboarded ? 'is-board' : '')}>
      {episode.published ? '已发布' : episode.render_complete ? '已生成，可发布' : episode.storyboarded ? '分镜已完成' : '待制作'}
    </em>}
  </li>)}</ol>;
}

export default function SegmentReview({episodes,collapsed=false,children}:
  {episodes: Episode[]; collapsed?: boolean; children?: ReactNode}) {
  if (!episodes.length) return null;
  const approved = episodes.some(episode => episode.storyboarded || episode.shots);
  const finished = episodes.filter(episode => episode.render_complete).length;
  const published = episodes.filter(episode => episode.published).length;
  if (collapsed) {
    return <details className="studio-note segment-review-collapsed">
      <summary>情节清单 · 共 {episodes.length} 幕 · 已完成 {finished} 幕 · 已发布 {published} 幕</summary>
      <EpisodeList episodes={episodes} approved/>
    </details>;
  }
  return <section className="segment-review" aria-label="情节确认">
    <h3>情节清单 · 共 {episodes.length} 幕</h3>
    <p>原文按因果切成了下面这些情节，制作会按这个顺序一幕一幕进行。确认之后才开始生成人物、服装与场景参考图；之后每一幕完成时，你都可以选择先发布，或继续下一幕。</p>
    <EpisodeList episodes={episodes} approved={approved}/>
    {children}
  </section>;
}
