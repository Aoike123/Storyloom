// 显示模型：轻量节点（id/kind/title/group/body/fields/isExample）。
// 不复刻完整业务数据（那属于未来业务层 D03/D04）；空状态优先，不伪造记录。
import { type Workspace } from './workspace-state';

export type NodeKind = 'source' | 'script' | 'cut' | 'scene' | 'shot' | 'asset' | 'clip';

// 节点内一个可编辑字段（剧本=动作/对白；切分=裁减正文）。value 为空表示待填写（空态，不伪造）。
export interface FieldDef {
  label: string;
  value: string | null;
}

export interface DisplayNode {
  id: string;
  kind: NodeKind;
  title: string;
  group?: string;
  // 原文参考：从来源片段截取的真实文本（只读，供节点内展示与对照）
  body?: string;
  // 可编辑字段（跟随所选对象，属性面板按此渲染真实字段，D 批）
  fields?: FieldDef[];
  isExample?: boolean;
}

export interface WorkspaceContent {
  workspace: Workspace;
  nodes: DisplayNode[];
  emptyReasons: Partial<Record<NodeKind, string>>;
}

// 节点类型的中文展示名（右侧摘要 / 空态用）
export const NODE_KIND_LABELS: Record<NodeKind, string> = {
  source: '来源故事',
  script: '剧本',
  cut: '裁减',
  scene: '场次',
  shot: '镜头',
  asset: '素材',
  clip: '成片',
};
