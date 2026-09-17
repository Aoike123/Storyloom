'use client';
import {useEffect, useRef, useState} from 'react';
import type {ComponentProps} from 'react';
import {TextReveal} from './textReveal';

// All visible paragraphs share one frame loop, and idle paragraphs stop subscribing.
const listeners = new Set<(now: number) => boolean>();
let frame: number | undefined;
function tick(now: number) {
  for (const listener of [...listeners]) if (!listener(now)) listeners.delete(listener);
  frame = listeners.size ? requestAnimationFrame(tick) : undefined;
}
function subscribe(listener: (now: number) => boolean) {
  listeners.add(listener);
  if (frame === undefined) frame = requestAnimationFrame(tick);
  return () => {listeners.delete(listener); if (!listeners.size && frame !== undefined) {cancelAnimationFrame(frame); frame = undefined;}};
}

export default function StreamText({text, active = false, streamKey, instant = false}: {text: string; active?: boolean; streamKey: string; instant?: boolean}) {
  const buffer = useRef<TextReveal | null>(null);
  if (!buffer.current) buffer.current = new TextReveal(text, streamKey, active && !instant);
  const [visible, setVisible] = useState({key: streamKey, text: buffer.current.shown});
  const textNow = visible.key === streamKey ? visible.text : active && !instant ? '' : text;

  useEffect(() => {
    const machine = buffer.current!;
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    machine.update(text, streamKey, active, performance.now(), instant || motion.matches || document.hidden);
    const publish = () => setVisible(current => current.key === streamKey && current.text === machine.shown ? current : {key: streamKey, text: machine.shown});
    publish();
    const unsubscribe = machine.shown !== machine.target ? subscribe(now => {const pending = machine.advance(now); publish(); return pending;}) : () => {};
    const showAll = () => {if (document.hidden || motion.matches) {machine.finish(); publish(); unsubscribe();}};
    document.addEventListener('visibilitychange', showAll);
    motion.addEventListener('change', showAll);
    return () => {unsubscribe(); document.removeEventListener('visibilitychange', showAll); motion.removeEventListener('change', showAll);};
  }, [text, active, streamKey, instant]);

  const typing = textNow !== text && !!text && !instant;
  return <span className="stream-text" aria-busy={typing || undefined}>{textNow}{typing && <i className="stream-text-cursor" aria-hidden="true"/>}</span>;
}

export function StreamRegion({active, streamKey, ...props}: ComponentProps<'div'> & {active: boolean; streamKey: string}) {
  const region = useRef<HTMLDivElement>(null), follow = useRef(true);
  const live = useRef(active), finishUntil = useRef(0);
  useEffect(() => {
    if (live.current && !active) finishUntil.current = performance.now() + 450;
    live.current = active;
  }, [active]);
  useEffect(() => {
    const element = region.current, content = element?.firstElementChild;
    if (!element || !content) return;
    follow.current = true;
    const observer = new ResizeObserver(() => {if (follow.current && (live.current || performance.now() < finishUntil.current)) element.scrollTop = element.scrollHeight;});
    observer.observe(content);
    return () => observer.disconnect();
  }, [streamKey]);
  // Marked while it is streaming so the author page can keep this text box in view instead of
  // scrolling the whole production module around it.
  return <div {...props} data-follow={active ? 'stream' : undefined} ref={region} onScroll={event => {
    const element = event.currentTarget;
    follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
    props.onScroll?.(event);
  }}/>;
}
