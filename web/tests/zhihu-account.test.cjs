const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');

function load(relative, modules={}) {
  const context={exports:{},require:name=>{
    if(name.endsWith('.css'))return {};
    if(Object.prototype.hasOwnProperty.call(modules,name))return modules[name];
    return require(name);
  }};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'..',relative),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022},
  }).outputText,context);
  return context.exports;
}

const account=load('app/zhihu-account.ts');
// The component imports its helpers by relative path, which the sandbox cannot resolve on its own.
const panel=load('app/author/ZhihuAccount.tsx',{'../zhihu-account':account}).default;

const wallet={uid:'525',beans:'500.00',granted:'500.00',
  costs:{llm:'1',image:'5',video:'48'},ledger:[]};

test('a signed-out visitor is offered login without losing the other two options',()=>{
  const html=renderToStaticMarkup(React.createElement(panel,{
    status:{configured:true,authorized:false,account:null,wallet:null},
    busy:false,onChanged:()=>{},next:'/author',
  }));
  assert.match(html,/用知乎账号登录/);
  assert.match(html,/共享额度/);
  assert.match(html,/自己的 Key/);
  assert.match(html,/href="\/api\/zhihu\/login\?next=%2Fauthor"/);
});

test('an unconfigured deployment says so instead of showing a dead button',()=>{
  const html=renderToStaticMarkup(React.createElement(panel,{
    status:{configured:false,authorized:false,account:null,wallet:null},
    busy:false,onChanged:()=>{},next:'/author',
  }));
  assert.match(html,/尚未在此部署配置/);
  assert.doesNotMatch(html,/api\/zhihu\/login/);
});

test('a signed-in account shows the nickname, balance and per-use costs',()=>{
  const html=renderToStaticMarkup(React.createElement(panel,{
    status:{configured:true,authorized:true,
      account:{uid:'525',fullname:'测试读者',avatar_path:null,headline:'一句话介绍',url:null,expires_at:0},
      wallet},
    busy:false,onChanged:()=>{},next:'/author',
  }));
  assert.match(html,/测试读者/);
  assert.match(html,/500 算力豆/);
  assert.match(html,/视频 48 豆／镜/);
  assert.doesNotMatch(html,/is-low/);
});

test('beans below one clip point at the alternative instead of only reporting a number',()=>{
  const html=renderToStaticMarkup(React.createElement(panel,{
    status:{configured:true,authorized:true,
      account:{uid:'525',fullname:'测试读者',avatar_path:null,headline:null,url:null,expires_at:0},
      wallet:{...wallet,beans:'20.00'}},
    busy:false,onChanged:()=>{},next:'/author',
  }));
  assert.match(html,/is-low/);
  assert.match(html,/算力豆不足一镜/);
  assert.match(html,/自己的 Key/);
});

test('the callback flag maps to readable copy and ignores anything unknown',()=>{
  assert.match(account.zhihuNotice('ok').text,/算力豆已到账/);
  assert.equal(account.zhihuNotice('ok').error,false);
  assert.match(account.zhihuNotice('denied').text,/取消了知乎授权/);
  assert.equal(account.zhihuNotice('denied').error,true);
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
  assert.equal(account.isLowBeans(wallet),false);
  assert.equal(account.isLowBeans({...wallet,beans:'47.99'}),true);
  assert.equal(account.isLowBeans({...wallet,beans:'48'}),false);
  assert.equal(account.isLowBeans(null),false);
});
