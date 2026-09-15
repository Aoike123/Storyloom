const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
function load(file){const ctx={exports:{},require};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,ctx);return ctx.exports.default;}
const ProviderError=load('ProviderError.tsx'),ImageRecovery=load('author/ImageRecovery.tsx');

test('provider explanation and trace are visible and escaped',()=>{
  const error={http_status:451,summary:'提示词或它要求的画面被服务商判定违规',advice:'请按供应商说明修改提示词后重试',source:'response',provider_message:'<script>provider detail</script>',request_id:'trace-123',provider_code:'policy'};
  const html=renderToStaticMarkup(React.createElement(ProviderError,{error}));
  assert.match(html,/HTTP 451/);assert.match(html,/trace-123/);assert.match(html,/policy/);
  assert.match(html,/提示词或它要求的画面被服务商判定违规/);
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
  const bare=renderToStaticMarkup(React.createElement(ProviderError,{error:{...error,provider_message:'',request_id:'',provider_code:''}}));
  assert.match(bare,/供应商未返回可展示的错误说明/);assert.doesNotMatch(bare,/供应商说明：/);
});

test('a picture stopped before submission shows its reason without inventing a provider response',()=>{
  const error={http_status:null,summary:'这次生图没有提交给供应商',advice:'运营方提供的该模型 Key 今日已被供应商暂停。请填写自己的 API Key 继续。',source:'not_submitted',provider_message:'',provider_code:'',request_id:''};
  const html=renderToStaticMarkup(React.createElement(ProviderError,{error}));
  assert.match(html,/没有提交给供应商/);assert.match(html,/API Key 继续/);
  assert.doesNotMatch(html,/HTTP/);assert.doesNotMatch(html,/供应商未返回可展示的错误说明/);
  assert.match(html,/换用可用的额度后可以直接重试这张图片/);
});

function buttons(tree){
  const found=[];
  (function walk(node){
    if(!node||typeof node!=='object')return;
    if(Array.isArray(node))return node.forEach(walk);
    if(node.type==='button')found.push(node);
    walk(node.props?.children);
  })(tree);
  return found;
}
const label=node=>Array.isArray(node.props.children)?node.props.children.join(''):String(node.props.children);

test('single-image retry requires an explicit enabled button click',()=>{
  const calls=[];const props={images:[{task_id:'failed-image',name:'公司急救培训室'}],busy:false,paid:true,onRetry:id=>calls.push(id)};
  const tree=ImageRecovery(props);assert.deepEqual(calls,[]);
  const [button]=buttons(tree);assert.equal(label(button),'重试这张图片');
  assert.equal(button.props.disabled,false);
  button.props.onClick();assert.deepEqual(calls,['failed-image']);
  for(const overrides of [{paid:false},{busy:true}]){
    const html=renderToStaticMarkup(React.createElement(ImageRecovery,{...props,...overrides}));assert.match(html,/<button[^>]+disabled/);
  }
  assert.equal(ImageRecovery({...props,images:[]}),null);
});

test('several rejected pictures can be brought back in one action',()=>{
  const retried=[];const all=[];
  const images=[{task_id:'one',name:'女主'},{task_id:'two',name:'公司急救培训室'},{task_id:'three',name:'走廊'}];
  const html=renderToStaticMarkup(React.createElement(ImageRecovery,{images,busy:false,paid:true,onRetry:id=>retried.push(id),onRetryAll:()=>all.push('all')}));
  assert.match(html,/全部重试这 3 张/);
  assert.deepEqual(retried,[]);assert.deepEqual(all,[]);
  const tree=ImageRecovery({images,busy:false,paid:true,onRetry:id=>retried.push(id),onRetryAll:()=>all.push('all')});
  const [batch,...perPicture]=buttons(tree);
  assert.equal(label(batch),'全部重试这 3 张');
  batch.props.onClick();assert.deepEqual(all,['all']);assert.equal(perPicture.length,3);
  perPicture[1].props.onClick();assert.deepEqual(retried,['two']);
  // One picture needs no batch action; a blocked round disables both ways.
  assert.doesNotMatch(renderToStaticMarkup(React.createElement(ImageRecovery,{images:images.slice(0,1),busy:false,paid:true,onRetry:()=>{}})),/全部重试/);
  const blocked=renderToStaticMarkup(React.createElement(ImageRecovery,{images,busy:false,paid:false,onRetry:()=>{},onRetryAll:()=>{}}));
  assert.match(blocked,/全部重试这 3 张/);assert.equal([...blocked.matchAll(/<button[^>]*disabled/g)].length,4);
});

test('a content-policy rejection hands the prompt back to the author',()=>{
  const retried=[];
  const refused={task_id:'refused',name:'身份 C004',category:'content_policy',needs_prompt_edit:true,
    prompt:'身份 C004：完整展示自然体表与身体结构，无体毛，不穿人类服装。'};
  const props={images:[refused],paid:true,busy:false,onRetry:(id,prompt)=>retried.push([id,prompt])};
  // The saved prompt is offered for editing, and the button stays disabled until it changes.
  const untouched=renderToStaticMarkup(React.createElement(ImageRecovery,props));
  assert.match(untouched,/供应商判定这条提示词要求的内容违规/);
  assert.match(untouched,/完整展示自然体表与身体结构/);
  assert.match(untouched,/<button[^>]+disabled[^>]*>用修改后的提示词重试/);
  assert.deepEqual(retried,[]);
  const [button]=buttons(ImageRecovery({...props,drafts:{refused:'身份 C004：该角色穿着中性基础短装，完整着装。'}}));
  assert.equal(button.props.disabled,false);
  button.props.onClick();
  assert.deepEqual(retried,[['refused','身份 C004：该角色穿着中性基础短装，完整着装。']]);
  // A short draft is refused, and the paid gate still applies.
  assert.match(renderToStaticMarkup(React.createElement(ImageRecovery,{...props,drafts:{refused:'太短'}})),/<button[^>]+disabled/);
  assert.match(renderToStaticMarkup(React.createElement(ImageRecovery,{...props,paid:false,drafts:{refused:'身份 C004：该角色穿着中性基础短装，完整着装。'}})),/<button[^>]+disabled/);
});
