// 只读适配器：把现有 work 对象映射为工作台展示模型（来源模型 + 五个工作区的轻量节点）。
// 纯函数、只读、不写库；缺失字段一律走空态，不伪造记录；示例片段/节点以 isExample 隔离。
import { type WorkspaceContent } from "./display-model";
import { type Workspace } from "./workspace-state";
import { buildSourceModel, type SourceModel } from "./source-model";

// 现有 work 对象的最小输入形状：只列 mapProject 会触碰的字段，全部可选（缺失即空态）
export interface WorkInput {
  id?: string;
  title?: string;
  stage?: string;
  source?: {
    work_id?: string;
    title?: string;
    content?: string;
    author_name?: string;
    completeness?: string;
  } | null;
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

// 工作台展示内容 = 来源模型 + 各工作区展示内容
export interface WorkbenchContent {
  source: SourceModel;
  byWorkspace: Record<Workspace, WorkspaceContent>;
}

const empty = (workspace: Workspace): WorkspaceContent => ({
  workspace,
  nodes: [],
  emptyReasons: {},
});

// 从情节内镜号 'G03-S02' 提取场次前缀 'G03'（后端 episodes 按此前缀分组发布）
function sceneOf(shotId?: string): string | undefined {
  const m = shotId ? /^(G\d{2})/.exec(shotId) : undefined;
  return m ? m[1] : undefined;
}

export function mapProject(work: WorkInput): WorkbenchContent {
  const source = work.source ?? null;
  const sourceText = source?.content ?? "";
  const sourceModel = buildSourceModel(
    sourceText,
    source?.completeness === "full" ? "import-1" : "import-0",
  );

  const cut = empty("cut");
  const script = empty("script");
  const board = empty("board");
  const assets = empty("assets");
  const film = empty("film");

  const items = work.creative?.items ?? [];
  const shots = (work.creative?.production?.shots ?? []).map((e) => ({
    shot: e.shot ?? null,
    clip: e.clip ?? null,
  }));

  // —— 切分／裁减：当前片段的裁减草稿。真实片段结构待 D03；此处仅占位，内容由组件按当前片段装配。
  cut.emptyReasons.script = "选择左侧片段后，在这里查看与裁减它的正文。";

  // —— 剧本：现有数据无独立剧本编排结构 → 空态，不伪造（示例剧本由组件按当前片段派生）。
  script.emptyReasons.script = "该片段尚未编排剧本";

  // —— 分镜：场次（按镜头 G 前缀分组）+ 逐镜头（真实数据）——
  const sceneCounts = new Map<string, number>();
  for (const { shot } of shots) {
    if (!shot?.id) continue;
    const g = sceneOf(shot.id);
    if (g) sceneCounts.set(g, (sceneCounts.get(g) ?? 0) + 1);
    board.nodes.push({
      id: shot.id,
      kind: "shot",
      title: shot.dramatic_action?.slice(0, 30) || shot.id,
      group: g,
    });
  }
  for (const [g, count] of sceneCounts)
    board.nodes.push({
      id: g,
      kind: "scene",
      title: `${g} · ${count} 镜`,
      group: g,
    });
  if (shots.length === 0) {
    board.emptyReasons.shot = "尚无分镜镜头";
    board.emptyReasons.scene = "该故事尚未编排场次";
  }

  // —— 资产：creative.items（角色 / 服装 / 场景背景，真实数据）——
  items.forEach((item, i) => {
    assets.nodes.push({
      id: item.asset?.id ?? item.task_id ?? `asset-${i}`,
      kind: "asset",
      title: item.name ?? item.asset?.name ?? "素材",
      group: item.role ?? undefined,
    });
  });
  if (items.length === 0)
    assets.emptyReasons.asset = "尚未准备角色 / 服装 / 场景素材";

  // —— 成片：shots[].clip（真实数据）。全尺寸剪辑组件在 C 批接入，此处先只读展示已生成片段。
  for (const { shot, clip } of shots) {
    if (clip?.id)
      film.nodes.push({
        id: clip.id,
        kind: "clip",
        title: clip.id,
        group: sceneOf(shot?.id),
      });
  }
  if (!shots.some((e) => e.clip?.id)) film.emptyReasons.clip = "尚无成片片段";

  return {
    source: sourceModel,
    byWorkspace: { cut, script, board, assets, film },
  };
}
