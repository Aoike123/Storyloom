export type ReleaseEntry = {
  occurrence_id?: string;
  clip_id: string;
  media: string;
  start: number;
  end: number;
  shot_id: string;
  status?: 'ready' | 'pending' | 'failed';
  kind?: 'original' | 'branch';
  label?: string;
  summary?: string;
  original_index?: number;
  message?: string;
};

export type Release = {
  id: string;
  source_work_id?: string;
  project_id?: string;
  title: string;
  source_title: string;
  author: string;
  description: string;
  entries: ReleaseEntry[];
  /** Whether this published version belongs to the currently signed-in account. */
  mine?: boolean;
  /** Publication time; the catalogue itself still defines display order. */
  created?: number;
  /** Public attribution snapshot for the maker of this particular version. */
  creator?: {name: string; avatar_path?: string | null};
};

export type ReaderBranch = {
  id: string;
  version: number;
  release_id: string;
  base_branch_id?: string;
  branch_version: number;
  status: 'planning' | 'generating' | 'ready' | 'rejected' | 'failed' | 'superseded';
  message: string;
  lock_forward_seek: boolean;
  intent: string;
  cut: {
    manifest_index: number;
    offset: number;
    original_index: number;
    original_shot_id?: string;
    source_kind?: 'original' | 'branch';
    at_story_end?: boolean;
  };
  summary?: string | null;
  terminal?: boolean | null;
  rejoin_index?: number | null;
  entries: ReleaseEntry[];
  ready_count: number;
  generated_count: number;
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
  // A public release's project may belong to another maker. Always enter by source story when one
  // exists: the server then creates or resumes this account's independent version of that story.
  const sourceId = item.work_id || item.source_work_id;
  return sourceId ? '/author?story=' + encodeURIComponent(sourceId)
    : item.project_id ? '/author?work=' + encodeURIComponent(item.project_id)
    : '/author';
};

export const canWatch = (item: CatalogItem) => !!item.release?.entries.length;
export const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/**
 * Group the catalogue by story.
 *
 * The catalogue carries every published production, so one story appears more than once when
 * several people have made it. Cards are stacked per story instead of repeating the same story as
 * unrelated cards. Productions keep the catalogue order, which is newest first.
 */
export function groupProductionsByStory(items: CatalogItem[], releases: Release[]) {
  const byStory: Record<string, Release[]> = {};
  for (const release of releases) {
    if (!release.source_work_id) continue;
    (byStory[release.source_work_id] ||= []).push(release);
  }
  return items.map(item => {
    const found = item.work_id ? byStory[item.work_id] || [] : [];
    const productions = found.length ? found : item.release ? [item.release] : [];
    return {item, productions, stacked: productions.length > 1};
  });
}
