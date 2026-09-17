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
  assert.deepEqual(selected,['style','segments_review']);
  assert.equal(steps[0].props['aria-current'],'step');
  assert.equal(steps[4].props.children.props.disabled,true);
  assert.equal(steps[5].props.children.props.disabled,true);
  assert.equal(steps[6].props.children.props.disabled,true);
  assert.equal(steps[7].props.children.props.disabled,true);
});

test('static displays remain static and navigation is disabled during a submission',()=>{
  assert.doesNotMatch(renderToStaticMarkup(React.createElement(WorkProgress,{stage:'assets_review'})),/<button/);
  const tree=WorkProgress({stage:'assets_review',disabled:true,onStepSelect:()=>{}});
  assert.ok(tree.props.children.every(step=>step.props.children.props.disabled));
});

test('production responsibilities are separate steps and later nodes stay locked',()=>{
  const tree=WorkProgress({stage:'storyboarding',onStepSelect:()=>{}});
  const steps=tree.props.children;
  assert.equal(steps.length,9);
  const labels=context.exports.workStages.map(stage=>stage.name);
  // 确认情节发生在绘制任何参考图之前，所以它排在准备形象之前。
  // 每完成一集就要决定发布还是继续，所以「本集发布」排在漫剧生成之后、审片验收之前。
  assert.deepEqual(Array.from(labels),['选择风格','确认情节','准备形象','确认图片','分镜生成','漫剧生成','本集发布','审片验收','发布作品']);
  assert.equal(steps[4].props.children.props.disabled,false);
  assert.equal(steps[5].props.children.props.disabled,true);
  assert.equal(steps[6].props.children.props.disabled,true);
  assert.equal(steps[7].props.children.props.disabled,true);
  assert.equal(steps[8].props.children.props.disabled,true);
});

test('the episode decision is a step of its own, so the rail still shows where the author is',()=>{
  const tree=WorkProgress({stage:'episode_review',onStepSelect:()=>{}});
  const steps=tree.props.children;
  assert.equal(steps[6].props.className,'is-current');
  assert.equal(steps[6].props['aria-current'],'step');
  // 已经有片段做完了，所以前面的步骤可以返回查看，后面的仍然锁着。
  assert.equal(steps[0].props.children.props.disabled,false);
  assert.equal(steps[5].props.children.props.disabled,false);
  assert.equal(steps[7].props.children.props.disabled,true);
  assert.equal(steps[8].props.children.props.disabled,true);
});
