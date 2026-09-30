const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');

const source=fs.readFileSync(path.join(__dirname,'../app/StoryImport.tsx'),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText;
const context={exports:{},require:name=>name.startsWith('./')?{default:()=>null}:require(name)};
vm.runInNewContext(code,context);
const {firstLineTitle}=context.exports;

test('the promised title fallback is the first non-empty line of the story',()=>{
  assert.equal(firstLineTitle('雨停在凌晨三点。\n\n便利店的白光落在街口。'), '雨停在凌晨三点。');
  assert.equal(firstLineTitle('\n  \n  第一句才是标题  \n第二句'), '第一句才是标题');
});

test('no usable line means no title, and long lines are clipped to the field limit',()=>{
  assert.equal(firstLineTitle('\n  \n'), undefined);
  assert.equal(firstLineTitle('字'.repeat(300)).length, 200);
});
