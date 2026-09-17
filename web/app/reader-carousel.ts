import type {CatalogItem} from './reader-types';
import {canWatch} from './reader-types';

const productionStages: Record<string, number> = {
  style: 1,
  segments_review: 2,
  preparing: 3,
  assets_review: 4,
  storyboarding: 5,
  rendering: 6,
  film_review: 7,
  published: 8,
  episode_review: 8,
};

export function productionRank(item: CatalogItem) {
  if (canWatch(item)) return 9;
  return productionStages[item.stage || ''] || (item.project_id ? .5 : 0);
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

/** The cover one step away in a direction, wrapping at either end. */
export function adjacentId(items: {id: string}[], current: number, direction: 1 | -1) {
  if (items.length < 2) return '';
  return items[wrapIndex(current + direction, items.length)]?.id || '';
}
