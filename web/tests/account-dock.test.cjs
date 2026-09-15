const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');

function load(relative, modules={}, globals={}) {
  const context={exports:{},require:name=>{
    if(name.endsWith('.css'))return {};
    if(Object.prototype.hasOwnProperty.call(modules,name))return modules[name];
    return require(name);
  },...globals};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'..',relative),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022},
  }).outputText,context);
  return context.exports;
}

// account-dock.ts imports the model-access helpers by relative path, which the sandbox cannot
// resolve on its own.
const modelAccess=load('app/model-access.ts');
// The transpiled module reads `window` from its own sandbox, so the fake browser goes in there.
const fakeWindow={};
class FakeCustomEvent{constructor(type,init){this.type=type;this.detail=init?.detail;}}
const dock=load('app/account-dock.ts',{'./model-access':modelAccess},{window:fakeWindow,CustomEvent:FakeCustomEvent});
const account=load('app/zhihu-account.ts',{'./model-access':modelAccess});
const types=load('app/reader-types.ts');

test('the dock renders the same label on the server as in the first client render',()=>{
  // The trigger used to read the own-key session during render, so the server sent "登录" while the
  // first client render said "使用自己的 Key" and React reported a hydration mismatch. The stored
  // hint may only be applied after mount.
  const React=require('react');
  const {renderToStaticMarkup}=require('react-dom/server');
  const reads=[];
  const access={
    readModelAccess:()=>{reads.push(1);return {token:'own-key-session',mode:'own',expires_at:Date.now()/1000+3600};},
    clearModelAccess:()=>{},
  };
  const icons=new Proxy({},{get:()=>()=>null});
  const zhihu={emptyZhihuStatus:{configured:false,authorized:false,account:null,wallet:null,can_generate:false,own_keys:false},
    readZhihuStatus:async()=>zhihu.emptyZhihuStatus,zhihuLoginLink:()=>'/api/zhihu/login',zhihuLogout:async()=>undefined};
  const helpers={OWN_KEY_PROVIDERS:[],announcePayerChanged:()=>{},clearOwnKeys:async()=>{},
    isBeansProblem:()=>false,onBeansProblem:()=>()=>{},saveOwnKeys:async()=>{}};
  const component=load('app/AccountDock.tsx',{
    'react/jsx-runtime':require('react/jsx-runtime'),'react':React,'lucide-react':icons,
    './model-access':access,'./zhihu-account':zhihu,'./account-dock':helpers,
  });
  const html=renderToStaticMarkup(React.createElement(component.default,{next:'/'}));
  assert.match(html,/aria-label="登录"/);
  assert.doesNotMatch(html,/使用自己的 Key/);
  // Rendering must not consult the browser's session at all: that is what made the two disagree.
  assert.deepEqual(reads,[]);
});

test('a wallet that cannot cover the next step is recognised from the server message',()=>{
  assert.equal(dock.isBeansProblem('算力豆不足：这一步需要 MiniMax 视频。请填写自己的 API Key 继续。'),true);
  assert.equal(dock.isBeansProblem('算力豆已用完'),true);
  assert.equal(dock.isBeansProblem('任务仍在运行。'),false);
  assert.equal(dock.isBeansProblem(''),false);
});

test('the beans signal reaches whoever is listening',()=>{
  const seen=[];
  const listeners={};
  fakeWindow.addEventListener=(name,handler)=>{listeners[name]=handler;};
  fakeWindow.removeEventListener=(name)=>{delete listeners[name];};
  fakeWindow.dispatchEvent=(event)=>{listeners[event.type]?.(event);};
  dock.announceBeansProblem('算力豆不足');
  assert.deepEqual(seen,[]);
  const stop=dock.onBeansProblem(detail=>seen.push(detail));
  dock.announceBeansProblem('算力豆不足');
  stop();
  assert.deepEqual(seen,['算力豆不足']);
});

test('announcing without a browser is harmless',()=>{
  // The helper is imported by pages that may render on the server.
  const bare=load('app/account-dock.ts',{'./model-access':modelAccess},{window:undefined,CustomEvent:undefined});
  assert.doesNotThrow(()=>bare.announceBeansProblem('算力豆不足'));
});

test('entering a story goes straight to the studio instead of a model-access detour',()=>{
  assert.equal(types.productionLink({work_id:'123'}),'/author?story=123');
  assert.equal(types.productionLink({work_id:'123',project_id:'somebody-elses-project'}),'/author?story=123');
  assert.equal(types.productionLink({source_work_id:'123',project_id:'somebody-elses-project'}),'/author?story=123');
  assert.equal(types.productionLink({project_id:'work_x'}),'/author?work=work_x');
  assert.equal(types.productionLink({}),'/author');
  assert.doesNotMatch(types.productionLink({work_id:'123'}),/setup/);
});

test('the callback flag maps to readable copy and ignores anything unknown',()=>{
  assert.match(account.zhihuNotice('ok').text,/算力豆已到账/);
  assert.equal(account.zhihuNotice('ok').error,false);
  assert.match(account.zhihuNotice('denied').text,/取消了知乎授权/);
  assert.match(account.zhihuNotice('error').text,/没有完成/);
  assert.equal(account.zhihuNotice(null),null);
  assert.equal(account.zhihuNotice('something-else'),null);
});

test('the login link only ever points back into this site',()=>{
  assert.equal(account.zhihuLoginLink('/author'),'/api/zhihu/login?next=%2Fauthor');
  for (const hostile of ['https://evil.example.com','//evil.example.com','javascript:alert(1)']) {
    const href=account.zhihuLoginLink(hostile);
    assert.match(href,/next=%2Fauthor$/,hostile);
    assert.doesNotMatch(href,/evil|javascript/);
  }
});

test('low balance is judged against one clip, not a fixed number',()=>{
  const wallet={uid:'525',beans:'500.00',granted:'500.00',costs:{llm:'1',image:'5',video:'48'},ledger:[]};
  assert.equal(account.isLowBeans(wallet),false);
  assert.equal(account.isLowBeans({...wallet,beans:'47.99'}),true);
  assert.equal(account.isLowBeans({...wallet,beans:'48'}),false);
  assert.equal(account.isLowBeans(null),false);
});

test('own-key status and removal carry the browser access token',async()=>{
  const calls=[];
  const access={
    modelAccessHeaders:()=>({'X-Storyloom-Model-Access':'own-key-session'}),
    saveModelAccess:()=>{},
  };
  const fetch=async(url,options={})=>{
    calls.push({url,options});
    return {ok:true,json:async()=>({configured:true,can_generate:true,own_keys:true})};
  };
  const statusModule=load('app/zhihu-account.ts',{'./model-access':access},{fetch});
  const dockModule=load('app/account-dock.ts',{'./model-access':access},{fetch,window:fakeWindow,CustomEvent:FakeCustomEvent});
  assert.equal((await statusModule.readZhihuStatus()).own_keys,true);
  await dockModule.clearOwnKeys();
  assert.equal(calls.length,2);
  for(const call of calls)assert.equal(call.options.headers['X-Storyloom-Model-Access'],'own-key-session');
  assert.equal(calls[1].options.method,'DELETE');
});
