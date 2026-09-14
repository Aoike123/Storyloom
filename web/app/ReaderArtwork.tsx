'use client';

import {useState} from 'react';

export default function ReaderArtwork({src, order, eager = false}: {src?: string; order: number; eager?: boolean}) {
  const [loaded, setLoaded] = useState('');
  const [failed, setFailed] = useState('');
  const valid = typeof src === 'string' && (src.startsWith('https://') || src.startsWith('/media/'));
  return <div className={'reader-artwork' + (loaded === src && failed !== src ? ' is-loaded' : '')} aria-hidden="true">
    <div className="reader-artwork-fallback"><span>{String(order + 1).padStart(2, '0')}</span><small>脑洞微小说</small></div>
    {valid && failed !== src && <img src={src} alt="" loading={eager ? 'eager' : 'lazy'} decoding="async"
      onLoad={() => setLoaded(src)} onError={() => setFailed(src)}/>}
  </div>;
}
