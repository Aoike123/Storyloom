'use client';
import { ChevronLeft } from 'lucide-react';
import { renderSegments, type SourceFragment, type SourceModel, type SourceView } from './source-model';
import { type Workspace } from './workspace-state';

interface Props {
  source: SourceModel;
  open: boolean;
  view: SourceView;
  activeFragmentId: string | null;
  active: Workspace;
  candidateFragments: SourceFragment[]; // A2：前端创建、未持久化的候选片段
  onToggleView: (view: SourceView) => void;
  onCollapse: () => void;
  onJump: (f: SourceFragment) => void;
}

// 左侧持续来源区（A1）：原文连续阅读 + 整块荧光区域；原文/片段两视图，同一稳定 id；
// 点击荧光区域或片段项 = 片段跳转（恢复目标片段的工作上下文）。来源区贯穿项目，跨工作区共享。
export default function SourceArea({ source, open, view, activeFragmentId, active, candidateFragments, onToggleView, onCollapse, onJump }: Props) {
  if (!open) return null;

  const fragments = [...source.fragments, ...candidateFragments].sort((a, b) => a.range.start - b.range.start);
  const byId = new Map(fragments.map((f) => [f.id, f]));
  const hasText = source.text.length > 0;
  const isFilm = active === 'film';
  const fragmentViewLabel = isFilm ? '片段资源' : '故事片段';
  const activeId = activeFragmentId;

  const segments = renderSegments(source.text, fragments, null);

  return (
    <aside className="workbench-source" aria-label="故事来源">
      <div className="workbench-source-head">
        <div className="workbench-source-title-wrap">
          <span className="workbench-source-title">故事来源</span>
          <small className="workbench-source-version">正文 {source.revision}</small>
        </div>
        <div className="workbench-source-tools">
          <button type="button" aria-pressed={view === 'original'} onClick={() => onToggleView('original')}>
            原文
          </button>
          <button type="button" aria-pressed={view === 'fragments'} onClick={() => onToggleView('fragments')}>
            {fragmentViewLabel}
          </button>
          <button type="button" className="icon" aria-label="收起来源区" title="收起来源区" onClick={onCollapse}>
            <ChevronLeft size={15} />
          </button>
        </div>
      </div>

      <div className="workbench-source-scroll">
        {!hasText ? (
          <div className="workbench-source-empty">该故事尚未导入原文。导入后在此连续阅读，并划选片段进入后续制作。</div>
        ) : view === 'original' ? (
          <div className="workbench-original">
            {segments.map((seg, i) =>
              seg.region ? (
                <span
                  key={i}
                  className={
                    'workbench-region' +
                    (seg.region.kind === 'pending' ? ' is-pending' : '') +
                    (activeId && seg.region.id === activeId ? ' is-active' : '')
                  }
                  role="button"
                  tabIndex={0}
                  onClick={() => {
                    const f = byId.get(seg.region!.id);
                    if (f) onJump(f);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      const f = byId.get(seg.region!.id);
                      if (f) onJump(f);
                    }
                  }}
                >
                  {seg.text}
                </span>
              ) : (
                <span key={i} className="workbench-gap">
                  {seg.text}
                </span>
              ),
            )}
          </div>
        ) : (
          <div className="workbench-fragments">
            {fragments.length === 0 ? (
              <div className="workbench-source-empty">尚无片段。在「原文」视图划选一段连续文字，建立第一个故事片段。</div>
            ) : (
              fragments.map((f, i) => (
                <button
                  key={f.id}
                  type="button"
                  className={'workbench-fragment' + (activeId === f.id ? ' is-active' : '')}
                  onClick={() => onJump(f)}
                >
                  <span className="workbench-fragment-index">{i + 1}</span>
                  <span className="workbench-fragment-body">
                    <strong>{f.name}</strong>
                    <small>
                      {f.origin} · {f.progress}
                    </small>
                    <span className={'workbench-fragment-status is-' + (f.status === '已确认' ? 'done' : 'todo')}>{f.status}</span>
                  </span>
                  {f.isExample ? <em className="workbench-fragment-example">示例</em> : null}
                </button>
              ))
            )}
          </div>
        )}
      </div>

      <div className="workbench-source-foot">
        {view === 'original' ? (isFilm ? '按故事片段取用视频资源' : '划选未标注文字可建立新片段') : '片段保留原文范围 · 选取与确认分别操作'}
      </div>
    </aside>
  );
}
