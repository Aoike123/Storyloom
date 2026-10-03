// F0 契约 §4 · 只读适配器：把现有 work 对象映射为四个工作区的轻量展示模型。
// 纯函数、只读、不写库；缺失字段一律走空态，不伪造记录；多镜头/多场次展示可带 isExample 隔离。
import { type WorkspaceContent } from './display-model';
import { type Workspace } from './workspace-state';

// 现有 work 对象的最小输入形状：只列 mapProject 会触碰的字段，全部可选（缺失即空态）
export interface WorkInput {
  id?: string;
  title?: string;
  stage?: string;
  source?: { work_id?: string; title?: string; content?: string; author_name?: string } | null;
  creative?: {
    items?: Array<{
      role?: string;
      name?: string;
      task_id?: string;
      asset?: { id?: string; name?: string } | null;
    }> | null;
    production?: {
      shots?: Array<{
        shot?: { id?: string; scene?: string; dramatic_action?: string } | null;
        clip?: { id?: string; duration?: number } | null;
      }> | null;
    } | null;
  } | null;
}

const empty = (workspace: Workspace): WorkspaceContent => ({ workspace, nodes: [], emptyReasons: {} });

// 从情节内镜号 'G03-S02' 提取场次前缀 'G03'（后端 episodes 按此前缀分组发布）
function sceneOf(shotId?: string): string | undefined {
  const m = shotId ? /^(G\d{2})/.exec(shotId) : undefined;
  return m ? m[1] : undefined;
}

export function mapProject(work: WorkInput): Record<Workspace, WorkspaceContent> {
  const story = empty('story');
  const production = empty('production');
  const assets = empty('assets');
  const release = empty('release');

  const source = work.source ?? null;
  const items = work.creative?.items ?? [];
  const shots = (work.creative?.production?.shots ?? []).map((e) => ({
    shot: e.shot ?? null,
    clip: e.clip ?? null,
  }));

  // —— 故事：来源微小说（项目的来源素材，非组织核心 / 归属根）——
  if (source && (source.title || source.content)) {
    story.nodes.push({
      id: source.work_id ?? work.id ?? 'source',
      kind: 'source',
      title: source.title || (source.content ? source.content.slice(0, 24) : '') || work.title || '来源故事',
    });
  } else {
    story.emptyReasons.source = '该项目暂无来源故事';
  }
  // 剧本：现有数据无独立"剧本编排"结构 → 明确空态，不伪造
  story.emptyReasons.script = '该故事尚未编排剧本';

  // —— 制作：场次（按镜头 G 前缀分组）+ 逐镜头 ——
  const sceneCounts = new Map<string, number>();
  for (const { shot } of shots) {
    if (!shot?.id) continue;
    const g = sceneOf(shot.id);
    if (g) sceneCounts.set(g, (sceneCounts.get(g) ?? 0) + 1);
    production.nodes.push({
      id: shot.id,
      kind: 'shot',
      title: shot.dramatic_action?.slice(0, 30) || shot.id,
      group: g,
    });
  }
  for (const [g, count] of sceneCounts) {
    production.nodes.push({ id: g, kind: 'scene', title: `${g} · ${count} 镜`, group: g });
  }
  if (shots.length === 0) {
    production.emptyReasons.shot = '尚无分镜镜头';
    production.emptyReasons.scene = '该故事尚未编排场次';
  }

  // —— 素材：creative.items（角色 / 服装 / 场景背景）——
  items.forEach((item, i) => {
    assets.nodes.push({
      id: item.asset?.id ?? item.task_id ?? `asset-${i}`,
      kind: 'asset',
      title: item.name ?? item.asset?.name ?? '素材',
      group: item.role ?? undefined,
    });
  });
  if (items.length === 0) assets.emptyReasons.asset = '尚未准备角色 / 服装 / 场景素材';

  // —— 成片：shots[].clip ——
  for (const { shot, clip } of shots) {
    if (clip?.id) {
      release.nodes.push({ id: clip.id, kind: 'clip', title: clip.id, group: sceneOf(shot?.id) });
    }
  }
  if (!shots.some((e) => e.clip?.id)) release.emptyReasons.clip = '尚无成片片段';

  return { story, production, assets, release };
}
