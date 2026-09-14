const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const filename = path.join(__dirname, '../app/author/projectProgress.ts');
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
const context = {exports: {}};
vm.runInNewContext(code, context);
const {mergeTask, mergeWorkspace} = context.exports;
const {viewedAuthorStage}=context.exports;
const {mergeProgress}=context.exports;
const {authorDisplayStage}=context.exports;
const {hasActiveProgress,shouldPollProgress}=context.exports;
const {progressStructure}=context.exports;
const {canRetryCurrentNode}=context.exports;

test('live progress uses one event stream without redundant workspace polling',()=>{
  const work={jobs:[],task:null,recommend_task_status:{id:'styles',status:'running'}};
  assert.equal(hasActiveProgress(work),true);
  assert.equal(shouldPollProgress('live',true),false);
  assert.equal(shouldPollProgress('connecting',true),false);
  assert.equal(shouldPollProgress('idle',true),false);
  assert.equal(shouldPollProgress('fallback',true),true);
  assert.equal(hasActiveProgress({...work,recommend_task_status:{id:'styles',status:'needs_review'}}),false);
});

test('a current production-node or child error exposes the retry action',()=>{
  assert.equal(canRetryCurrentNode({production_steps:[{id:'storyboarding',task:{status:'needs_review'}}],jobs:[]},'storyboarding'),true);
  assert.equal(canRetryCurrentNode({production_steps:[{id:'storyboarding',task:{status:'waiting'}}],jobs:[{production_phase:'storyboarding',status:'failed'}]},'storyboarding'),true);
  assert.equal(canRetryCurrentNode({production_steps:[{id:'storyboarding',task:{status:'waiting'}}],jobs:[]},'storyboarding'),false);
  assert.equal(canRetryCurrentNode({production_steps:[{id:'storyboarding',task:{status:'needs_review'}}],jobs:[]},'rendering'),false);
});

test('busy status changes stay on the event stream while terminal results request one full refresh',()=>{
  const queued={run_id:'round',stage:'style',jobs:[],task:null,recommend_task_status:{id:'styles',status:'queued'}};
  const running={...queued,recommend_task_status:{id:'styles',status:'running'}};
  const completed={...queued,recommend_task_status:{id:'styles',status:'completed'}};
  assert.equal(progressStructure(queued),progressStructure(running));
  assert.notEqual(progressStructure(running),progressStructure(completed));
});

test('a delayed full-page response cannot erase a newly completed preview or a new task', () => {
  const current = {id:'work', progress_at:20, jobs:[{id:'new', status:'completed'}], outputs:[{id:'new', media:'/media/new.png'}]};
  const incoming = {id:'work', progress_at:10, jobs:[], outputs:[], source:{content:'original'}};
  const merged = mergeWorkspace(current, incoming);
  assert.equal(merged.jobs, current.jobs);
  assert.equal(merged.outputs, current.outputs);
  assert.equal(merged.source, incoming.source);
});
test('an intentional newer revision can replace an old preview', () => {
  const current = {id:'work', progress_at:10, jobs:[], outputs:[{id:'old'}]};
  const incoming = {id:'work', progress_at:20, jobs:[], outputs:[{id:'new'}]};
  assert.equal(mergeWorkspace(current, incoming).outputs, incoming.outputs);
});
test('completed task state and newer summaries survive older snapshots', () => {
  const completed = {id:'task', status:'completed', activity:{updated_at:20}};
  assert.equal(mergeTask(completed, {id:'task', status:'running', activity:{updated_at:20}}), completed);
  assert.equal(mergeTask(completed, {id:'task', status:'completed', activity:{updated_at:10}}), completed);
});
test('a resumed task replaces its previous needs-review state', () => {
  const stopped = {id:'task', status:'needs_review', message:'旧校验错误', activity:{updated_at:20}};
  const resumed = {id:'task', status:'running', message:'正在继续处理', activity:{updated_at:20}};
  assert.equal(mergeTask(stopped, resumed), resumed);
});
test('switching work never carries summaries or previews across stories', () => {
  const incoming = {id:'other', progress_at:1, jobs:[], outputs:[]};
  assert.equal(mergeWorkspace({id:'work', progress_at:20, outputs:[{id:'private'}]}, incoming), incoming);
});

test('earlier steps can be revisited without unlocking future steps',()=>{
  assert.equal(viewedAuthorStage('assets_review','style'),'style');
  assert.equal(viewedAuthorStage('assets_review','preparing'),'preparing');
  assert.equal(viewedAuthorStage('assets_review','published'),'assets_review');
  assert.equal(viewedAuthorStage('assets_review','unknown'),'assets_review');
});

test('legacy producing records display the saved responsibility without replaying work',()=>{
  assert.equal(authorDisplayStage({stage:'producing',creative:{stage:'fittings_review'}}),'storyboarding');
  assert.equal(authorDisplayStage({stage:'producing',creative:{stage:'storyboarding'}}),'storyboarding');
  assert.equal(authorDisplayStage({stage:'producing',creative:{stage:'videos_review'}}),'rendering');
  assert.equal(viewedAuthorStage('storyboarding','rendering'),'storyboarding');
  assert.equal(viewedAuthorStage('rendering','compositing'),'storyboarding');
});

test('a delayed old-round snapshot cannot restore invalidated progress or assets',()=>{
  const current={id:'work',run_id:'new-round',stage:'preparing',progress_at:20,director_id:'new-director',creative:{items:[]},jobs:[],outputs:[]};
  const old={id:'work',run_id:'old-round',stage:'published',progress_at:10,director_id:'old-director',release_id:'old-release',creative:{items:['stale']},outputs:[{id:'stale'}]};
  const merged=mergeWorkspace(current,old);
  assert.equal(merged.run_id,'new-round');assert.equal(merged.stage,'preparing');
  assert.equal(merged.director_id,'new-director');assert.equal(merged.creative,current.creative);
  assert.equal(merged.outputs,current.outputs);
  assert.equal(merged.release_id,undefined);
});

test('a faster progress event cannot prevent a completed edit from replacing its asset card',()=>{
  const original={id:'work',run_id:'round',workspace_at:10,progress_at:10,creative:{version:1,items:[{task_id:'old',asset:{media:'/media/old.png'}}]},jobs:[{id:'old',status:'completed'}],outputs:[{id:'old'}]};
  const event={id:'work',run_id:'round',progress_at:30,jobs:[{id:'new',status:'completed'}],outputs:[{id:'new',media:'/media/new.png'}]};
  const streamed=mergeProgress(original,event);
  assert.equal(streamed.workspace_at,10);
  const full={...original,workspace_at:29,progress_at:29,creative:{version:2,items:[{task_id:'new',asset:{media:'/media/new.png'}}]},jobs:[{id:'new',status:'running'}],outputs:[]};
  const merged=mergeWorkspace(streamed,full);
  assert.equal(merged.creative.items[0].asset.media,'/media/new.png');
  assert.equal(merged.creative.version,2);assert.equal(merged.workspace_at,29);
  assert.equal(merged.jobs[0].status,'completed');assert.equal(merged.outputs[0].id,'new');
  assert.equal(mergeWorkspace(merged,original).creative,merged.creative);
});

test('progress events from a different round cannot attach its assets or metadata',()=>{
  const current={id:'work',run_id:'new',workspace_at:10,progress_at:10,jobs:[],outputs:[]};
  const event={id:'work',run_id:'old',progress_at:30,jobs:[{id:'old'}],outputs:[{id:'old'}],creative:{items:['old']}};
  assert.equal(mergeProgress(current,event),current);
});

test('terminal progress advances the visible workflow stage without another workspace request',()=>{
  const current={id:'work',run_id:'round',stage:'preparing',display_stage:'preparing',progress_at:10,jobs:[],outputs:[]};
  const event={id:'work',run_id:'round',stage:'assets_review',progress_at:20,jobs:[],outputs:[]};
  const merged=mergeProgress(current,event);
  assert.equal(merged.stage,'assets_review');
  assert.equal(merged.display_stage,'assets_review');
});
