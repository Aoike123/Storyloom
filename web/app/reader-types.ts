export type Release = {
  id: string;
  source_work_id?: string;
  project_id?: string;
  title: string;
  source_title: string;
  author: string;
  description: string;
  entries: {clip_id: string; media: string; start: number; end: number; shot_id: string}[];
};

export type CatalogItem = {
  id: string;
  work_id: string | null;
  title?: string;
  description?: string;
  artwork?: string;
  tab_artwork?: string;
  labels: string[];
  release: Release | null;
  project_id?: string;
  stage?: string;
};

export type Catalog = {
  items: CatalogItem[];
  releases: Release[];
  count: number;
  brainstorm_count: number;
  ready_count: number;
  catalog_available: boolean;
  stale: boolean;
  cached: boolean;
  fetched_at: number | null;
  warning?: string;
};

export const productionLink = (item: {work_id?: string | null; source_work_id?: string; project_id?: string | null}) => {
  const author = item.project_id ? '/author?work=' + encodeURIComponent(item.project_id)
    : item.work_id || item.source_work_id ? '/author?story=' + encodeURIComponent((item.work_id || item.source_work_id)!)
    : '/author';
  return '/setup?next=' + encodeURIComponent(author);
};

export const canWatch = (item: CatalogItem) => !!item.release?.entries.length;
export const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
