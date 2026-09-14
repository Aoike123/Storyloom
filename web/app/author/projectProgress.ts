import type {ProgressTask} from '../ProgressFeedback';

const activeStatuses = ['queued', 'running', 'waiting'];
const finalStatuses = ['completed', 'cancelled', 'superseded'];

export function hasActiveProgress(work: any) {
  return [...(work?.jobs || []), work?.task, work?.recommend_task_status].some(
    task => task && !['legacy_trial', 'legacy_composition'].includes(task.production_phase || '')
      && activeStatuses.includes(task.status),
  );
}

export function shouldPollProgress(connection: string, tracking: boolean) {
  return tracking && connection === 'fallback';
}

export function mergeTask(prior: ProgressTask | null | undefined, incoming: ProgressTask | null | undefined) {
  if (!prior || !incoming || prior.id !== incoming.id) return incoming;
  const priorUpdate = Math.max(prior.activity?.updated_at || 0, prior.result?.live?.updated_at || 0);
  const nextUpdate = Math.max(incoming.activity?.updated_at || 0, incoming.result?.live?.updated_at || 0);
  if (priorUpdate > nextUpdate || (finalStatuses.includes(prior.status) && activeStatuses.includes(incoming.status))) return prior;
  return incoming;
}

export function mergeWorkspace(current: any, incoming: any) {
  if (!current || current.id !== incoming.id) return incoming;
  const currentSnapshot = current.workspace_at ?? current.progress_at ?? 0;
  const incomingSnapshot = incoming.workspace_at ?? incoming.progress_at ?? 0;
  if (currentSnapshot > incomingSnapshot) {
    if ((current.run_id || null) !== (incoming.run_id || null)) return current;
    return {...incoming, ...current};
  }
  const snapshot = {...incoming, workspace_at: incomingSnapshot};
  if ((current.run_id || null) !== (incoming.run_id || null)) return snapshot;
  // Progress events have no asset cards. Their newer timestamps must not discard
  // the latest full snapshot containing a replacement image and its specification.
  if ((current.progress_at || 0) > (incoming.progress_at || 0)) return mergeProgress(snapshot,current);
  const tasks = new Map<string, ProgressTask>((current.jobs || []).map((task: ProgressTask) => [task.id, task]));
  return {...snapshot, task: mergeTask(current.task, incoming.task),
    recommend_task_status: mergeTask(current.recommend_task_status, incoming.recommend_task_status),
    jobs: (incoming.jobs || []).map((task: ProgressTask) => mergeTask(tasks.get(task.id), task))};
}

export function mergeProgress(current:any, incoming:any) {
  if (!current || current.id!==incoming.id || (current.run_id || null)!==(incoming.run_id || null)
      || (current.progress_at || 0)>(incoming.progress_at || 0)) return current;
  const tasks=new Map<string,ProgressTask>((current.jobs || []).map((task:ProgressTask)=>[task.id,task]));
  return {...current,progress_at:incoming.progress_at,
    task:mergeTask(current.task,incoming.task),
    recommend_task_status:mergeTask(current.recommend_task_status,incoming.recommend_task_status),
    jobs:(incoming.jobs || []).map((task:ProgressTask)=>mergeTask(tasks.get(task.id),task)),
    outputs:incoming.outputs,production_steps:incoming.production_steps};
}

export function progressStructure(work: any) {
  return JSON.stringify([work?.run_id,work?.stage, ...[work?.task, work?.recommend_task_status, ...(work?.jobs || [])]
    .filter(Boolean).map(task => [task.id, activeStatuses.includes(task.status) ? 'active' : task.status])]);
}

export const authorStageIds=['style','preparing','assets_review','storyboarding','rendering','film_review','published'];
export function authorDisplayStage(work:any) {
  if(work?.display_stage&&work.display_stage!=='compositing')return work.display_stage;
  if(work?.stage==='compositing'||work?.display_stage==='compositing')return 'storyboarding';
  if(work?.stage!=='producing')return work?.stage;
  const saved=work?.creative?.stage;
  if(['assets_review','fittings_review','trials_review'].includes(saved))return 'storyboarding';
  if(['composites_ready','references_ready','storyboarding'].includes(saved))return 'storyboarding';
  return 'rendering';
}
export function viewedAuthorStage(actual:string|undefined,requested:string|null) {
  if(actual==='producing')actual='rendering';
  if(actual==='compositing')actual='storyboarding';
  if(requested==='producing')requested='rendering';
  if(requested==='compositing')requested='storyboarding';
  const target=authorStageIds.indexOf(requested || '');
  return target>=0 && target<=authorStageIds.indexOf(actual || '') ? requested! : actual;
}
