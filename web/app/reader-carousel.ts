import type {CatalogItem} from './reader-types';
import {canWatch} from './reader-types';

const productionStages: Record<string, number> = {
  style: 1,
  preparing: 2,
  assets_review: 3,
  storyboarding: 4,
  compositing: 4,
  rendering: 5,
  producing: 5,
  film_review: 6,
  published: 7,
};

export function productionRank(item: CatalogItem) {
  // A verified, playable release is the strongest signal that a work is complete.
  if (canWatch(item)) return 8;
  return productionStages[item.stage || ''] || (item.project_id ? 0.5 : 0);
}

export function rankByProduction(items: CatalogItem[]) {
  return items
    .map((item, index) => ({item, index}))
    .sort((a, b) => productionRank(b.item) - productionRank(a.item) || a.index - b.index)
    .map(({item}) => item);
}

export function wrapIndex(index: number, length: number) {
  return length > 0 ? ((index % length) + length) % length : 0;
}

export function circularOffset(index: number, current: number, length: number) {
  if (length <= 0) return 0;
  const forward = wrapIndex(index - current, length);
  const backward = forward - length;
  return Math.abs(backward) < Math.abs(forward) ? backward : forward;
}
