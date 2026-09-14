'use client';

import {useState} from 'react';

export default function ReaderArtwork({src, order, eager = false, priority = false, showNumber = true}: {src?: string; order: number; eager?: boolean; priority?: boolean; showNumber?: boolean}) {
  const [loaded, setLoaded] = useState('');
  const [failed, setFailed] = useState('');
  const valid = typeof src === 'string' && (src.startsWith('https://') || src.startsWith('/media/'));
  return <div className={'reader-artwork' + (loaded === src && failed !== src ? ' is-loaded' : '')} aria-hidden="true">
    <div className="reader-artwork-fallback">{showNumber && <span>{String(order + 1).padStart(2, '0')}</span>}<small>脑洞微小说</small></div>
    {valid && failed !== src && <img src={src} alt="" loading={eager || priority ? 'eager' : 'lazy'} fetchPriority={priority ? 'high' : 'auto'} decoding="async"
      onLoad={() => setLoaded(src)} onError={() => setFailed(src)}/>}
  </div>;
}
