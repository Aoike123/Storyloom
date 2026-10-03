'use client';
import { useEffect, useState } from 'react';
import { Play, Pause, Film, Plus, Trash2, ArrowUp, ArrowDown } from 'lucide-react';
import { type DisplayNode } from './display-model';

type TrackId = 'video' | 'audio' | 'text';
const TRACKS: { id: TrackId; label: string }[] = [
  { id: 'video', label: '视频' },
  { id: 'audio', label: '音频' },
  { id: 'text', label: '字幕' },
];

interface PlacedClip {
  id: string;
  name: string;
  isExample: boolean;
}

interface Props {
  clips: DisplayNode[]; // 项目已生成的成片片段（真实数据，若存在）
  resources: { id: string; name: string }[]; // 来源片段 = 可插入的视频资源
  isExample: boolean; // 整体是否示例（无真实成片时）
}

const TOTAL = 96; // 秒（示例时长；真实时长来自成片元数据，D07 待定）
const fmt = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

// 成片工作区：全尺寸剪辑组件（独立实例，非画布）。
// 16:9 监视器 + 播放/seek；多轨时间轴（视频/音频/字幕）+ 播放头；来源资源区插入/重排/删除。
// 本期为前端示例：播放/seek/插入/重排/删除均为本地状态，不接真实剪辑内核/持久化（D03/D07 待定）。
export default function FilmEditor({ clips, resources, isExample }: Props) {
  const seeded: PlacedClip[] =
    clips.length > 0
      ? clips.map((c) => ({ id: c.id, name: c.title, isExample: c.isExample ?? false }))
      : [
          { id: 'ex-clip-1', name: '示例镜头 · 开场', isExample: true },
          { id: 'ex-clip-2', name: '示例镜头 · 对白', isExample: true },
        ];
  const [tracks, setTracks] = useState<Record<TrackId, PlacedClip[]>>({ video: seeded, audio: [], text: [] });
  const [playhead, setPlayhead] = useState(0); // 0..1
  const [playing, setPlaying] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // 播放：推进播放头（前端模拟；无真实媒体内核）
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => setPlayhead((p) => (p + 0.5 / TOTAL >= 1 ? 0 : p + 0.5 / TOTAL)), 500);
    return () => clearInterval(timer);
  }, [playing]);

  function insert(r: { id: string; name: string }) {
    const clip: PlacedClip = { id: `rc-${r.id}-${Date.now()}`, name: r.name, isExample: true };
    setTracks((t) => ({ ...t, video: [...t.video, clip] }));
    setSelectedId(clip.id);
  }

  // 定位已选 clip 的轨道与下标
  let selected: { track: TrackId; idx: number; clip: PlacedClip } | null = null;
  if (selectedId) {
    for (const t of TRACKS) {
      const idx = tracks[t.id].findIndex((c) => c.id === selectedId);
      if (idx >= 0) {
        selected = { track: t.id, idx, clip: tracks[t.id][idx] };
        break;
      }
    }
  }

  function move(dir: -1 | 1) {
    if (!selected) return;
    const { track, idx } = selected;
    const list = tracks[track];
    const j = idx + dir;
    if (j < 0 || j >= list.length) return;
    const next = [...list];
    [next[idx], next[j]] = [next[j], next[idx]];
    setTracks((t) => ({ ...t, [track]: next }));
  }

  function remove() {
    if (!selected) return;
    const { track, idx } = selected;
    setTracks((t) => ({ ...t, [track]: t[track].filter((_, i) => i !== idx) }));
    setSelectedId(null);
  }

  const currentClip = tracks.video.find((c) => c.id === selectedId) ?? tracks.video[0] ?? null;

  return (
    <div className="workbench-film">
      <div className="workbench-film-bar">
        <strong>成片剪辑</strong>
        {isExample ? <small className="workbench-film-note">示例剪辑 · 未写库（D03/D07 待定）</small> : null}
      </div>

      <div className="workbench-film-monitor">
        <div className="workbench-film-screen">
          <Film size={34} />
          <span>{currentClip ? currentClip.name : '预览画面'}</span>
        </div>
        <div className="workbench-film-transport">
          <button type="button" className="workbench-film-play" onClick={() => setPlaying((p) => !p)} aria-label={playing ? '暂停' : '播放'}>
            {playing ? <Pause size={16} /> : <Play size={16} />}
          </button>
          <span className="workbench-film-time">
            {fmt(playhead * TOTAL)} / {fmt(TOTAL)}
          </span>
          <input
            type="range"
            className="workbench-film-seek"
            min={0}
            max={1000}
            value={Math.round(playhead * 1000)}
            aria-label="播放位置"
            onChange={(e) => setPlayhead(Number(e.target.value) / 1000)}
          />
        </div>
      </div>

      <div className="workbench-film-body">
        <div className="workbench-film-resources">
          <h4>来源片段 · 可插入</h4>
          {resources.length === 0 ? (
            <p className="workbench-film-res-empty">尚无来源片段</p>
          ) : (
            <ul>
              {resources.map((r) => (
                <li key={r.id}>
                  <span className="workbench-film-res-name">{r.name}</span>
                  <button type="button" className="workbench-film-res-add" onClick={() => insert(r)} aria-label={`插入 ${r.name}`}>
                    <Plus size={14} />
                    插入
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="workbench-film-timeline">
          <div className="workbench-film-playhead" style={{ left: `${playhead * 100}%` }} aria-hidden="true" />
          {TRACKS.map((t) => (
            <div key={t.id} className="workbench-film-track">
              <span className="workbench-film-track-label">{t.label}</span>
              <div className="workbench-film-track-clips">
                {tracks[t.id].map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    className={
                      'workbench-film-clip' +
                      (selectedId === c.id ? ' is-selected' : '') +
                      (c.isExample ? ' is-example' : '')
                    }
                    onClick={() => setSelectedId(c.id)}
                  >
                    {c.name}
                  </button>
                ))}
                {tracks[t.id].length === 0 ? <span className="workbench-film-track-empty">（空）</span> : null}
              </div>
            </div>
          ))}
          {selected ? (
            <div className="workbench-film-clip-tools">
              <span>
                已选「{selected.clip.name}」 · {TRACKS.find((t) => t.id === selected.track)?.label}轨
              </span>
              <button type="button" onClick={() => move(-1)} aria-label="上移">
                <ArrowUp size={14} />
              </button>
              <button type="button" onClick={() => move(1)} aria-label="下移">
                <ArrowDown size={14} />
              </button>
              <button type="button" className="is-danger" onClick={remove} aria-label="删除">
                <Trash2 size={14} />
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
