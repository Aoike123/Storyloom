'use client';
import {useState} from 'react';
import {ChevronDown, ChevronRight} from 'lucide-react';
import {TaskProgress, activeStatuses, problemStatuses, type ProgressTask} from '../ProgressFeedback';

/**
 * The activity list follows the production tree instead of flattening it.
 *
 * Every job used to be printed in one stream, so a finished style task, a failed storyboard node and
 * the reviews underneath it looked like peers and the steps appeared to be missing. Here a
 * coordinator (风格推荐 / 分镜生成 / 漫剧生成) owns a row and the work it dispatched is nested
 * under it, hidden behind a disclosure until it needs attention.
 */
const NODE_KINDS = ['author_flow', 'author_storyboard', 'author_render', 'author_styles'];

export type ActivityRow = {node: ProgressTask | null; children: ProgressTask[]};

const isProblem = (task: ProgressTask) => problemStatuses.includes(task.status);

export function activityRows(tasks: ProgressTask[]): ActivityRow[] {
  const nodes = tasks.filter(task => NODE_KINDS.includes(task.kind));
  const claimed = new Set<string>();
  const rows: ActivityRow[] = nodes.map(node => {
    const phase = node.production_phase;
    const children = tasks.filter(task => {
      if (NODE_KINDS.includes(task.kind)) return false;
      const owner = task.production_node;
      const mine = owner ? owner === node.id : !!phase && task.production_phase === phase;
      if (mine) claimed.add(task.id);
      return mine;
    });
    return {node, children};
  });
  // Work that belongs to no coordinator still gets shown: it is either older than the split or a
  // task the backend has not attached to a node yet.
  const orphans = tasks.filter(task => !NODE_KINDS.includes(task.kind) && !claimed.has(task.id));
  if (orphans.length) rows.push({node: null, children: orphans});
  return rows;
}

function NodeRow({row}: {row: ActivityRow}) {
  const trouble = row.children.filter(isProblem).length;
  const running = row.children.some(task => activeStatuses.includes(task.status));
  // A disclosure that opens itself when something needs attention, so the failure is never hidden.
  const [open, setOpen] = useState(false);
  const expanded = open || trouble > 0;
  if (!row.node) {
    return <div className="activity-children">{row.children.map(task => <TaskProgress task={task} key={task.id}/>)}</div>;
  }
  return <section className={'activity-node' + (trouble ? ' has-problem' : '')} aria-label={(row.node.label || '制作节点') + '与其任务'}>
    <TaskProgress task={row.node}/>
    {row.children.length > 0 && <button type="button" className="activity-node-toggle" aria-expanded={expanded}
      onClick={() => setOpen(value => !value)}>
      {expanded ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}
      <span>本节点的 {row.children.length} 项任务</span>
      <small>{trouble ? trouble + ' 项需处理' : running ? '进行中' : '已完成'}</small>
    </button>}
    {expanded && <div className="activity-children">{row.children.map(task => <TaskProgress task={task} key={task.id}/>)}</div>}
  </section>;
}

export default function ProductionActivity({tasks}: {tasks: ProgressTask[]}) {
  const rows = activityRows(tasks);
  return <div className="author-task-history activity-tree">{rows.map((row, index) =>
    <NodeRow row={row} key={row.node?.id || 'orphans-' + index}/>)}</div>;
}
