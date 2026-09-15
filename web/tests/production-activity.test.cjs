const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');

function load(relative,modules={}){
  const context={exports:{},require:name=>{
    if(name.endsWith('.css'))return {};
    if(Object.prototype.hasOwnProperty.call(modules,name))return modules[name];
    return require(name);
  }};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'..',relative),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022},
  }).outputText,context);
  return context.exports;
}

const icons=new Proxy({},{get:()=>()=>null});
// ProgressFeedback pulls in the streaming and activity panels; the tree only needs the row.
const reveal=load('app/textReveal.ts');
const stream=load('app/StreamText.tsx',{'./textReveal':reveal});
const activityRow={default:()=>null};
const feedback=load('app/ProgressFeedback.tsx',{
  'lucide-react':icons,'./StreamText':stream,'./TaskActivity':activityRow,'./StyleProgress':activityRow,
  './ProviderError':activityRow,
});
const activity=load('app/author/ProductionActivity.tsx',{
  'lucide-react':icons,'../ProgressFeedback':feedback,'./TaskActivity':{default:()=>null},
});

const task=overrides=>({id:'t',kind:'image',status:'completed',...overrides});

test('the activity list nests dispatched work under its coordinator',()=>{
  const jobs=[
    task({id:'node',kind:'author_storyboard',status:'needs_review',production_phase:'storyboarding'}),
    task({id:'child-a',kind:'director',production_node:'node',status:'completed'}),
    task({id:'child-b',kind:'image',production_node:'node',status:'needs_review'}),
  ];
  const rows=activity.activityRows(jobs);
  assert.equal(rows.length,1);
  assert.equal(rows[0].node.id,'node');
  assert.deepEqual(rows[0].children.map(item=>item.id),['child-a','child-b']);
});

test('work with no coordinator still gets shown instead of disappearing',()=>{
  const jobs=[
    task({id:'node',kind:'author_render',status:'running',production_phase:'rendering'}),
    task({id:'loose',kind:'director',status:'completed'}),
  ];
  const rows=activity.activityRows(jobs);
  assert.deepEqual(rows.map(row=>row.node&&row.node.id),[ 'node', null ]);
  assert.deepEqual(rows[1].children.map(item=>item.id),['loose']);
});

test('a node with a failed child expands itself so the failure is never hidden',()=>{
  const jobs=[
    task({id:'node',kind:'author_storyboard',status:'waiting',production_phase:'storyboarding'}),
    task({id:'child-a',kind:'director',production_node:'node',status:'completed'}),
    task({id:'child-b',kind:'director',production_node:'node',status:'needs_review',message:'S03 镜号不连续',activity:null}),
  ];
  const html=renderToStaticMarkup(React.createElement(activity.default,{tasks:jobs}));
  assert.match(html,/本节点的 2 项任务/);
  assert.match(html,/1 项需处理/);
  // The failure row is rendered without a click: a hidden error is what made the steps look missing.
  assert.match(html,/S03 镜号不连续/);
});

test('a healthy node keeps its children behind the disclosure',()=>{
  const jobs=[
    task({id:'node',kind:'author_render',status:'running',production_phase:'rendering'}),
    task({id:'child-a',kind:'video',production_node:'node',status:'completed',message:'片段已保存'}),
  ];
  const html=renderToStaticMarkup(React.createElement(activity.default,{tasks:jobs}));
  assert.match(html,/本节点的 1 项任务/);
  assert.match(html,/已完成/);
  assert.doesNotMatch(html,/片段已保存/);
});
