const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const context={exports:{},require:name=>name.endsWith('.css')?{}:require(name)};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app/GenerationPrompt.tsx'),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX},
}).outputText,context);

test('asset revision diagnostics show the feedback and full prompt regeneration mode',()=>{
  const html=renderToStaticMarkup(React.createElement(context.exports.default,{generation:{
    source:'request',prompt:'独立服装三视图。灰色针织上衣，透明水彩绘制，粗纹水彩纸纹理。',
    input_mode:'text_to_image',revision_instruction:'把上衣改成灰色',regeneration_mode:'full_prompt',
    model:'Tongyi-MAI/Z-Image-Turbo',
  }}));
  assert.match(html,/把上衣改成灰色/);assert.match(html,/已整合完整提示词重新生成/);
  assert.match(html,/Tongyi-MAI\/Z-Image-Turbo/);assert.match(html,/透明水彩绘制/);
  assert.doesNotMatch(html,/图片编辑/);assert.doesNotMatch(html,/参考图/);
});
