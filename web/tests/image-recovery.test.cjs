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

test('provider explanation and trace are visible and escaped; legacy does not invent a reason',()=>{
  const error={http_status:451,summary:'生图请求被服务商拒绝',advice:'请查看供应商说明',source:'response',provider_message:'<script>provider detail</script>',request_id:'trace-123',provider_code:'policy'};
  const html=renderToStaticMarkup(React.createElement(ProviderError,{error}));
  assert.match(html,/HTTP 451/);assert.match(html,/trace-123/);assert.match(html,/policy/);
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
  const legacy=renderToStaticMarkup(React.createElement(ProviderError,{error:{...error,source:'legacy_status',provider_message:'',request_id:'',provider_code:''}}));
  assert.match(legacy,/历史任务未保存原始响应/);assert.doesNotMatch(legacy,/供应商说明：/);
});

test('single-image retry requires an explicit enabled button click',()=>{
  const calls=[];const props={images:[{task_id:'failed-image',name:'公司急救培训室'}],busy:false,paid:true,onRetry:id=>calls.push(id)};
  const tree=ImageRecovery(props);assert.deepEqual(calls,[]);
  const button=tree.props.children[2][0].props.children[1];assert.equal(button.props.disabled,false);
  button.props.onClick();assert.deepEqual(calls,['failed-image']);
  for(const overrides of [{paid:false},{busy:true}]){
    const html=renderToStaticMarkup(React.createElement(ImageRecovery,{...props,...overrides}));assert.match(html,/<button[^>]+disabled/);
  }
  assert.equal(ImageRecovery({...props,images:[]}),null);
});
