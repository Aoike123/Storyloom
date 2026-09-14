const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');

const source=fs.readFileSync(path.join(__dirname,'../app/ProgressFeedback.tsx'),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const context={exports:{},require:name=>name.startsWith('./')?{default:()=>null}:require(name)};
vm.runInNewContext(code,context);
const {WorkProgress}=context.exports;

test('completed steps are real buttons and navigate to the selected earlier step',()=>{
  const selected=[];
  const tree=WorkProgress({stage:'assets_review',viewedStage:'style',onStepSelect:step=>selected.push(step)});
  const steps=tree.props.children;
  const first=steps[0].props.children,second=steps[1].props.children;
  assert.equal(first.type,'button');assert.equal(first.props.disabled,false);
  assert.equal(second.props.disabled,false);
  first.props.onClick();second.props.onClick();
  assert.deepEqual(selected,['style','preparing']);
  assert.equal(steps[0].props['aria-current'],'step');
  assert.equal(steps[3].props.children.props.disabled,true);
  assert.equal(steps[4].props.children.props.disabled,true);
  assert.equal(steps[5].props.children.props.disabled,true);
});

test('static displays remain static and navigation is disabled during a submission',()=>{
  assert.doesNotMatch(renderToStaticMarkup(React.createElement(WorkProgress,{stage:'assets_review'})),/<button/);
  const tree=WorkProgress({stage:'assets_review',disabled:true,onStepSelect:()=>{}});
  assert.ok(tree.props.children.every(step=>step.props.children.props.disabled));
});

test('production responsibilities are separate steps and later nodes stay locked',()=>{
  const tree=WorkProgress({stage:'storyboarding',onStepSelect:()=>{}});
  const steps=tree.props.children;
  assert.equal(steps.length,7);
  const labels=context.exports.workStages.map(stage=>stage.name);
  assert.deepEqual(Array.from(labels.slice(3,5)),['分镜生成','漫剧生成']);
  assert.equal(steps[3].props.children.props.disabled,false);
  assert.equal(steps[4].props.children.props.disabled,true);
  assert.equal(steps[5].props.children.props.disabled,true);
  assert.equal(steps[6].props.children.props.disabled,true);
});
