const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');

function load(file,globals={}){
  const context={exports:{},require,...globals};
  const code=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app/author',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  vm.runInNewContext(code,context);return context.exports;
}
const AssetFeedback=load('AssetFeedback.tsx').default;
const base={name:'服装参考图',task:{id:'current-image',status:'completed',kind:'image'},text:'衣服改成黑色',paid:false,busy:false,inFlight:false,browsingEarlier:false,onTextChange:()=>{},onPaidChange:()=>{},onSubmit:()=>{}};
const button=tree=>tree.props.children.find(child=>child?.type==='button');

test('a filled feedback card shows its permission control and enables after confirmation',()=>{
  let paid=false,submitted=0;
  const props={...base,onPaidChange:allowed=>{paid=allowed;},onSubmit:()=>submitted++};
  let tree=AssetFeedback(props);
  assert.equal(button(tree).props.disabled,true);
  button(tree).props.onClick();assert.equal(submitted,0);
  const html=renderToStaticMarkup(React.createElement(AssetFeedback,props));
  assert.match(html,/本卡片的模型调用许可/);assert.match(html,/允许本作品调用付费模型/);
  const permission=tree.props.children.find(child=>child?.props?.className?.includes('asset-feedback-permission'));
  permission.props.children[0].props.onChange({target:{checked:true}});
  tree=AssetFeedback({...props,paid});assert.equal(button(tree).props.disabled,false);
  button(tree).props.onClick();assert.equal(submitted,1);
});

test('unavailable feedback actions explain the specific blocking condition',()=>{
  for(const [override,reason] of [
    [{text:''},'修改意见'],[{busy:true},'正在提交'],[{inFlight:true},'本轮任务正在执行'],
    [{browsingEarlier:true},'返回当前进度'],[{task:null},'尚未准备好'],
    [{task:{...base.task,status:'superseded'}},'当前不能修改'],
  ]){
    const tree=AssetFeedback({...base,paid:true,...override});
    assert.equal(button(tree).props.disabled,true);
    assert.ok(button(tree).props.title.includes(reason));
    assert.ok(renderToStaticMarkup(tree).includes(reason));
  }
});

test('the studio page owns the single permission control for every asset card',()=>{
  const props={...base,showPermission:false};
  const tree=AssetFeedback(props);
  assert.equal(tree.props.children.some(child=>child?.props?.className?.includes('asset-feedback-permission')),false);
  const html=renderToStaticMarkup(React.createElement(AssetFeedback,props));
  assert.doesNotMatch(html,/type="checkbox"/);
  assert.doesNotMatch(html,/本卡片的模型调用许可/);
  assert.match(html,/请先开启本页的模型调用许可/);
  assert.equal(button(tree).props.disabled,true);
  assert.equal(button(AssetFeedback({...props,paid:true})).props.disabled,false);
});

test('explicit permission survives reload in the same tab and stays scoped to the work',()=>{
  const values=new Map();const sessionStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};
  const first=load('useModelPermission.ts',{sessionStorage});
  assert.equal(first.readModelPermission('work-a'),false);
  first.saveModelPermission('work-a',true);
  const reloaded=load('useModelPermission.ts',{sessionStorage});
  assert.equal(reloaded.readModelPermission('work-a'),true);
  assert.equal(reloaded.readModelPermission('work-b'),false);
  reloaded.saveModelPermission('work-a',false);
  assert.equal(first.readModelPermission('work-a'),false);
});
