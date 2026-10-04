"use client";

import { useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { reducedMotion } from "./reader-types";

const EXIT_MS = 160;

function keyed(children: ReactNode): Map<string, ReactNode> {
  const map = new Map<string, ReactNode>();
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child && typeof child === "object" && "key" in child)
      map.set(String((child as { key: unknown }).key), child);
  }
  return map;
}

export default function ReaderStoryGrid({
  ids,
  children,
}: {
  ids: string[];
  children: ReactNode;
}) {
  const grid = useRef<HTMLDivElement>(null);
  const positions = useRef(new Map<string, { x: number; y: number }>());
  // Everything still in the DOM on the previous render: survivors plus cards that are fading
  // out. A fast re-filter recomputes leavers against this set, so a ghost never fades twice
  // and nothing vanishes without its exit beat.
  const rendered = useRef(new Map<string, ReactNode>());
  const exitTimer = useRef(0);
  const order = ids.join("|");
  const [exiting, setExiting] = useState<ReactNode[]>([]);

  useLayoutEffect(() => {
    const current = keyed(children);
    const previousAll = rendered.current;
    const leaverKeys = [...previousAll.keys()].filter(
      (key) => !current.has(key),
    );
    const all = new Map(current);
    for (const key of previousAll.keys())
      if (!current.has(key)) all.set(key, previousAll.get(key)!);
    rendered.current = all;

    if (!reducedMotion() && leaverKeys.length) {
      window.clearTimeout(exitTimer.current);
      setExiting(leaverKeys.map((key) => previousAll.get(key)!));
      exitTimer.current = window.setTimeout(() => {
        setExiting([]);
        for (const key of leaverKeys) rendered.current.delete(key);
      }, EXIT_MS + 16);
    } else {
      window.clearTimeout(exitTimer.current);
    }

    const previous = positions.current;
    const next = new Map<string, { x: number; y: number }>();
    for (const card of Array.from(
      grid.current?.children || [],
    ) as HTMLElement[]) {
      const id = card.dataset.storyId;
      if (!id) continue; // exit wrappers carry no story id and are not part of the FLIP set
      const point = { x: card.offsetLeft, y: card.offsetTop };
      next.set(id, point);
      card.getAnimations().forEach((animation) => animation.cancel());
      if (reducedMotion() || !previous.size) continue;
      const old = previous.get(id);
      if (old && (old.x !== point.x || old.y !== point.y)) {
        card.animate(
          [
            {
              transform: `translate(${old.x - point.x}px, ${old.y - point.y}px)`,
            },
            { transform: "translate(0, 0)" },
          ],
          { duration: 200, easing: "cubic-bezier(.22,1,.36,1)" },
        );
      } else if (!old) {
        card.animate(
          [
            { opacity: 0, transform: "translateY(5px)" },
            { opacity: 1, transform: "translate(0)" },
          ],
          { duration: 180, easing: "cubic-bezier(.22,1,.36,1)" },
        );
      }
    }
    positions.current = next;
  }, [order]);

  return (
    <div className="reader-story-grid" ref={grid}>
      {children}
      {exiting.map((node, i) => (
        <div
          key={
            "exit-" +
            (node && typeof node === "object" && "key" in node
              ? (node as { key: unknown }).key
              : i)
          }
          className="reader-exit-clip"
        >
          {node}
        </div>
      ))}
    </div>
  );
}
