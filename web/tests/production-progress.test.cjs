const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const context={exports:{},require:name=>name==='../ProgressFeedback'?{
  activeStatuses:['queued','running','waiting'],taskNames:{image:'画面'},
  TaskProgress:({task})=>React.createElement('div',{'data-focused-task':task.id},task.label),
}:name==='../GenerationPrompt'?{default:()=>null}:require(name)};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app/author/ProductionProgress.tsx'),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022},
}).outputText,context);

test('cancelled trials and superseded outputs are not offered as current production tasks',()=>{
  const html=renderToStaticMarkup(React.createElement(context.exports.default,{stage:'assets_review',connection:'live',tasks:[
    {id:'initial',kind:'image',status:'completed',label:'初版图片'},
    {id:'old-edit',kind:'image',status:'superseded',revision_of:'initial',label:'已替换图片'},
    {id:'current',kind:'image',status:'completed',revision_of:'old-edit',label:'当前图片'},
    {id:'cancelled-trial',kind:'image',status:'cancelled',label:'取消的试拍'},
  ]}));
  assert.match(html,/data-focused-task="current"/);
  assert.doesNotMatch(html,/初版图片|已替换图片|取消的试拍/);
});
