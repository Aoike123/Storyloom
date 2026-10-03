'use client';

import { useEffect, useRef, useState, type SyntheticEvent } from 'react';
import './watch.css';

interface ReleaseEntry {
  id?: string;
  media?: string;
  start?: number;
  end?: number;
  status?: string;
}

interface Release {
  id: string;
  project_id?: string;
  title?: string;
  author?: string;
  description?: string;
  creator?: { name?: string } | string | null;
  entries: ReleaseEntry[];
  published_at?: number;
  units_complete?: boolean;
}

// A standalone, no-login viewer for a public project's finished film. The homepage's public
// detail links here as /watch?release=<id>; the backend only serves a release whose project
// is public, so no authentication is needed on this page.
export default function Watch() {
  const [rid, setRid] = useState('');
  const [release, setRelease] = useState<Release | null>(null);
  const [index, setIndex] = useState(0);
  const [error, setError] = useState('');
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    setRid(new URL(window.location.href).searchParams.get('release') || '');
  }, []);

  useEffect(() => {
    if (!rid) return;
    setRelease(null);
    setError('');
    fetch(`/api/author/releases/${encodeURIComponent(rid)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('http_' + r.status))))
      .then((data: Release) => setRelease(data))
      .catch(() => setError('这部成片尚未公开，或已不存在。'));
  }, [rid]);

  const entries = release?.entries ?? [];
  const entry = entries[index];
  const creatorName = typeof release?.creator === 'string' ? release.creator : release?.creator?.name;

  const onLoadedMetadata = (e: SyntheticEvent<HTMLVideoElement>) => {
    const v = e.currentTarget;
    if (entry && v.duration && (entry.start || 0) < v.duration) v.currentTime = entry.start || 0;
    v.play().catch(() => {});
  };

  const onTimeUpdate = (e: SyntheticEvent<HTMLVideoElement>) => {
    const v = e.currentTarget;
    if (!entry || v.paused) return;
    if (v.currentTime >= (entry.end ?? 0) - 0.04 && index + 1 < entries.length) setIndex((i) => i + 1);
  };

  return (
    <main className="watch">
      <header className="watch-topbar">
        <a className="brand" href="/">叙间</a>
        <span className="watch-hint">公开成片 · 免登录观看</span>
      </header>
      {error ? (
        <div className="watch-empty">{error}</div>
      ) : !release ? (
        <div className="watch-empty">加载中…</div>
      ) : (
        <div className="watch-body">
          {entry && entry.media ? (
            <video
              key={release.id + ':' + (entry.id ?? index)}
              ref={video}
              src={entry.media}
              controls
              playsInline
              preload="auto"
              autoPlay
              className="watch-video"
              onLoadedMetadata={onLoadedMetadata}
              onTimeUpdate={onTimeUpdate}
            />
          ) : (
            <div className="watch-empty">这部成片还没有可播放的片段。</div>
          )}
          <div className="watch-meta">
            <h1 className="watch-title">{release.title || '未命名成片'}</h1>
            {(creatorName || release.author) && (
              <div className="watch-by">
                {release.author ? `原作 ${release.author}` : '原作未知'}
                {creatorName ? ` · 制作 ${creatorName}` : ''}
              </div>
            )}
            {release.description && <p className="watch-desc">{release.description}</p>}
          </div>
          {entries.length > 1 && (
            <nav className="watch-eps">
              {entries.map((e, i) => (
                <button
                  key={e.id ?? i}
                  type="button"
                  className={'watch-ep' + (i === index ? ' is-current' : '')}
                  onClick={() => setIndex(i)}
                  disabled={i > index && e.status === 'pending'}
                >
                  第 {i + 1} 集
                </button>
              ))}
            </nav>
          )}
        </div>
      )}
    </main>
  );
}
