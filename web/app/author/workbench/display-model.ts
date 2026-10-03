// F0 契约 §4 · 显示模型：轻量节点（id/kind/title/group/isExample）。
// 不复刻完整业务数据（那属于未来业务层 D03/D04）；空状态优先，不伪造记录。
import { type Workspace } from './workspace-state';

export type NodeKind = 'source' | 'script' | 'scene' | 'shot' | 'asset' | 'clip';

export interface DisplayNode {
  id: string;
  kind: NodeKind;
  title: string;
  group?: string;
  isExample?: boolean;
}

export interface WorkspaceContent {
  workspace: Workspace;
  nodes: DisplayNode[];
  emptyReasons: string[];
}
