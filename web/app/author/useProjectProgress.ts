'use client';
import {useEffect, useRef, useState} from 'react';
import type {Dispatch, SetStateAction} from 'react';
import {mergeProgress, progressStructure} from './projectProgress';

export default function useProjectProgress(work: any, enabled: boolean, setWork: Dispatch<SetStateAction<any>>, refresh: () => void) {
  const [connection, setConnection] = useState('connecting');
  const latest = useRef(work);
  latest.current = work;
  const id = work?.id as string | undefined;
  useEffect(() => {
    if (!enabled || !id) return;
    let alive = true, stream: EventSource | undefined, refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let structure = progressStructure(latest.current);
    function connect() {
      stream?.close();
      if (!alive || document.hidden) return;
      setConnection('connecting');
      stream = new EventSource('/api/author/projects/' + encodeURIComponent(id!) + '/events');
      stream.onopen = () => {if (alive) setConnection('live');};
      stream.onmessage = event => {
        if (!alive) return;
        try {
          const data = JSON.parse(event.data);
          if (data.id !== id || !Array.isArray(data.jobs)) return;
          setWork((current: any) => mergeProgress(current,data));
          const nextStructure = progressStructure(data);
          if (nextStructure !== structure) {
            structure = nextStructure;
            clearTimeout(refreshTimer);
            refreshTimer = setTimeout(() => {if (alive) refresh();}, 180);
          }
        } catch {setConnection('fallback');}
      };
      // EventSource reconnects read-only; the regular workspace poll remains available.
      stream.onerror = () => {if (alive) setConnection('fallback');};
    }
    connect();
    document.addEventListener('visibilitychange', connect);
    return () => {alive = false; stream?.close(); clearTimeout(refreshTimer); document.removeEventListener('visibilitychange', connect);};
  }, [id, enabled, setWork, refresh]);
  return connection;
}
