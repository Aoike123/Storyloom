'use client';

import {useLayoutEffect, useRef} from 'react';
import type {ReactNode} from 'react';
import {reducedMotion} from './reader-types';

export default function ReaderStoryGrid({ids, children}: {ids: string[]; children: ReactNode}) {
  const grid = useRef<HTMLDivElement>(null);
  const positions = useRef(new Map<string, {x: number; y: number}>());
  const order = ids.join('|');

  useLayoutEffect(() => {
    const previous = positions.current;
    const next = new Map<string, {x: number; y: number}>();
    for (const card of Array.from(grid.current?.children || []) as HTMLElement[]) {
      const id = card.dataset.storyId!;
      const point = {x: card.offsetLeft, y: card.offsetTop};
      next.set(id, point);
      card.getAnimations().forEach(animation => animation.cancel());
      if (reducedMotion() || !previous.size) continue;
      const old = previous.get(id);
      if (old && (old.x !== point.x || old.y !== point.y)) {
        card.animate([{transform: `translate(${old.x - point.x}px, ${old.y - point.y}px)`}, {transform: 'translate(0, 0)'}], {duration: 200, easing: 'cubic-bezier(.2,.7,.2,1)'});
      } else if (!old) {
        card.animate([{opacity: 0, transform: 'translateY(5px)'}, {opacity: 1, transform: 'translateY(0)'}], {duration: 180, easing: 'ease-out'});
      }
    }
    positions.current = next;
  }, [order]);

  return <div className="reader-story-grid" ref={grid}>{children}</div>;
}
