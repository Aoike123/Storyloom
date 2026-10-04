"use client";
import { useEffect, useRef, useState } from "react";

type Layer = { src: string; exit: boolean; first: boolean };

/**
 * One asset, one fixed box. A new picture fades in over 160ms; when a newer version replaces
 * the current one, the two crossfade over 220ms inside the same frame, so the grid around it
 * never moves while layers change. Layers keep stable keys and a stable class once mounted, so
 * pruning the exited layer never remounts or restarts the animation of the layer that stays.
 */
export default function AssetFrame({
  src,
  alt,
  video = false,
  controls = false,
}: {
  src: string;
  alt: string;
  video?: boolean;
  controls?: boolean;
}) {
  const [layers, setLayers] = useState<Layer[]>([
    { src, exit: false, first: true },
  ]);
  const timer = useRef(0);
  useEffect(() => {
    setLayers((current) => {
      if (current[0].src === src) return current;
      return [
        { src, exit: false, first: false },
        { ...current[0], exit: true },
      ];
    });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(
      () => setLayers((current) => current.filter((_, i) => i === 0)),
      260,
    );
  }, [src]);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const render = (layer: Layer) => {
    const cls =
      "asset-layer" +
      (layer.exit ? " is-exiting" : layer.first ? " is-first" : " is-entering");
    const key = layer.exit ? layer.src + "/exit" : layer.src;
    return video ? (
      <video
        key={key}
        src={layer.src}
        className={cls}
        controls={controls && !layer.exit}
        playsInline
        preload="metadata"
        aria-label={alt}
      />
    ) : (
      <img
        key={key}
        src={layer.src}
        alt={layer.exit ? "" : alt}
        className={cls}
        loading="lazy"
      />
    );
  };
  return (
    <div className={"asset-frame" + (video ? " asset-frame-video" : "")}>
      {[...layers].reverse().map(render)}
    </div>
  );
}
