'use client';
import {ArrowRight, ListOrdered} from 'lucide-react';

type Episode = {
  segment_id: string; title?: string; beat?: string; source_refs?: string[]; shot_budget?: number;
  characters?: string[]; location?: string; purpose?: string; continuity_out?: string;
  source_text?: string; storyboarded?: boolean; render_complete?: boolean; published?: boolean;
  shots?: number; clips?: number;
};

/**
 * The cut, before any artwork exists.
 *
 * The cut decides both the episodes and which characters and places are worth drawing, so it is the
 * author who confirms it: nothing is generated until they accept the list. The same panel later
 * shows how far each episode has come, which is what makes 发布 / 继续 a choice rather than a guess.
 */
export default function SegmentReview({episodes,paid,busy,onApprove,readOnly=false}:
  {episodes: Episode[]; paid: boolean; busy: boolean; onApprove: () => void; readOnly?: boolean}) {
  if (!episodes.length) return null;
  const approved = episodes.some(episode => episode.storyboarded || episode.shots);
  const finished = episodes.filter(episode => episode.render_complete).length;
  const published = episodes.filter(episode => episode.published).length;
  return <section className="asset-redesign segment-review" aria-label="情节确认">
    <h3>情节清单 · 共 {episodes.length} 幕</h3>
    <p>原文按因果切成了下列情节，制作会按这个顺序一幕一幕进行。开始生成人物与场景之前，请先核对切割是否符合你的预期。</p>
    <ol className="segment-review-list">{episodes.map((episode, index) => <li key={episode.segment_id}>
      <header>
        <strong>{episode.segment_id} · {episode.title || '未命名情节'}</strong>
        <span>{episode.beat || '剧情段落'} · {(episode.source_refs || []).join('、')} · {episode.shot_budget || 0} 镜</span>
      </header>
      <p>{episode.purpose}</p>
      <small>出场：{(episode.characters || []).join('、') || '未点名'} · 场景：{episode.location || '未指明'} · 结尾：{episode.continuity_out || '—'}</small>
      {episode.source_text && <blockquote>{episode.source_text}</blockquote>}
      {(approved) && <em className={'segment-state ' + (episode.published ? 'is-published' : episode.render_complete ? 'is-ready' : episode.storyboarded ? 'is-board' : '')}>
        {episode.published ? '已发布' : episode.render_complete ? '已生成，可发布' : episode.storyboarded ? '分镜已完成' : '待制作'}
      </em>}
    </li>)}</ol>
    {approved
      ? <p className="studio-note">已完成 {finished} / {episodes.length} 幕，其中 {published} 幕已经发布。第 {Math.min(published + 1, episodes.length)} 幕开始前，仍可回到这里核对。</p>
      : <div className="asset-redesign-heading"><strong>确认这份切割，开始逐幕制作</strong>
          <button type="button" className="button primary" disabled={busy || !paid} onClick={onApprove}>
            <ListOrdered size={15}/>确认情节，开始制作 <ArrowRight size={14}/>
          </button>
        </div>}
    <small>{readOnly ? '当前在回看已完成步骤。' : !paid ? '勾选上方付费调用后可开始。' : approved ? '未开始的情节会按顺序继续。' : '确认后才会生成人物、服装与场景参考图。'}</small>
  </section>;
}
