const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

const source = fs.readFileSync(path.join(__dirname, '../app/TaskActivity.tsx'), 'utf8');
const code = ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
}}).outputText;
const errorCode=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app/ProviderError.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const errorContext={exports:{},require};vm.runInNewContext(errorCode,errorContext);
const revealContext={exports:{}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app/textReveal.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,revealContext);
const streamContext={exports:{},require:name=>name==='./textReveal'?revealContext.exports:require(name)};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app/StreamText.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,streamContext);
const context = {exports: {}, require: name => name === './GenerationPrompt' ? {default: () => null} : name === './StreamText' ? streamContext.exports : name === './ProviderError' ? errorContext.exports : name === './NodeSkills' ? {SkillCallTrace: () => null} : require(name)};
vm.runInNewContext(code, context);
const TaskActivity = context.exports.default;

function render(status) {
  return renderToStaticMarkup(React.createElement(TaskActivity, {detailed: true, task: {
    id: 'design', kind: 'art_design', status, message: '场景高度校验提示',
    activity: {type: 'model', title: '设计独立服装与物理场景', phase: 'validating',
      started_at: 1, updated_at: 2, summary: '', history: [], events: [],
      items: [{title: '幸福之家大楼外部入口', text: ''}]},
  }}));
}

test('a paused or failed design does not leave an active drafting placeholder', () => {
  for (const status of ['needs_review', 'failed']) {
    const html = render(status);
    assert.doesNotMatch(html, /这一部分正在展开/);
    assert.match(html, /任务已暂停/);
    assert.match(html, /场景高度校验提示/);
  }
});

test('only an active design has the drafting placeholder', () => {
  assert.match(render('running'), /这一部分正在展开/);
  assert.doesNotMatch(render('completed'), /这一部分正在展开/);
});

test('stopped and superseded work are historical records, not attention errors',()=>{
  for(const status of ['cancelled','superseded']){
    const html=render(status);
    assert.doesNotMatch(html,/has-problem|需处理|查看错误提示|这一部分正在展开/);
    assert.match(html,status==='cancelled'?/已停止/:/已失效/);
  }
});

test('a completed image edit displays its new result rather than the input reference',()=>{
  const task={id:'edit-image',kind:'image',status:'completed',revision_of:'original',preview:'/media/original.png',result:{media:'/media/edited.png'},
    activity:{type:'media',title:'服装参考图',phase:'saving',started_at:1,updated_at:2,summary:'',items:[],history:[],events:[]}};
  const html=renderToStaticMarkup(React.createElement(TaskActivity,{task,detailed:true}));
  assert.match(html,/src="\/media\/edited.png"/);
  assert.doesNotMatch(html,/src="\/media\/original.png"/);
  assert.match(html,/本次生成的参考图/);
});
