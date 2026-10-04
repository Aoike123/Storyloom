"use client";
import { useEffect, useRef, useState } from "react";
import { reducedMotion } from "./reader-types";

/**
 * One-time arrival for a section: it reveals on its first intersection with the viewport and
 * the observer then unobserves the element, so there is no continuous bookkeeping while the
 * section sits in view. Reduced motion reveals immediately with no observer at all.
 */
export function useReveal<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [revealed, setRevealed] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element || revealed) return;
    if (reducedMotion() || typeof IntersectionObserver === "undefined") {
      setRevealed(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setRevealed(true);
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.15 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [revealed]);
  return [ref, revealed] as const;
}
