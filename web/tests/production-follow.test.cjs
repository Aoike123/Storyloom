const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');

function load(relative,modules={},globals={}){
  const context={exports:{},require:name=>Object.prototype.hasOwnProperty.call(modules,name)?modules[name]:require(name),...globals};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'..',relative),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022},
  }).outputText,context);
  return context.exports;
}

const follow=load('app/author/useProductionFollow.ts');
const reveal=load('app/textReveal.ts');
const stream=load('app/StreamText.tsx',{'./textReveal':reveal});
const StreamRegion=stream.StreamRegion;

test('the followed box is the streaming text, not the whole module',()=>{
  const module={id:'module'};
  assert.equal(follow.followAnchor({querySelectorAll:()=>[],id:'module'}).id,'module',
    'without a marked box the module itself is followed');
  const summary={id:'summary'},drafts={id:'drafts'};
  // The later box in reading order wins: it is the one that keeps growing.
  assert.equal(follow.followAnchor({querySelectorAll:()=>[summary,drafts]}),drafts);
  assert.equal(follow.followAnchor({querySelectorAll:()=>[summary]}),summary);
});

test('a streaming region marks itself for following and stops when it finishes',()=>{
  const active=renderToStaticMarkup(React.createElement(StreamRegion,{active:true,streamKey:'t'},
    React.createElement('p',null,'正在返回的内容')));
  assert.match(active,/data-follow="stream"/);
  const finished=renderToStaticMarkup(React.createElement(StreamRegion,{active:false,streamKey:'t'},
    React.createElement('p',null,'已经完成的内容')));
  assert.doesNotMatch(finished,/data-follow/);
});

test('following eases toward the box instead of jumping to it',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../app/author/useProductionFollow.ts'),'utf8');
  // A single eased loop chases a moving box; instant re-scrolls on every resize are what made the
  // page move in visible steps while the answer was typed.
  assert.match(source,/window\.scrollTo\(0,window\.scrollY\+distance\*EASE\)/);
  assert.match(source,/requestAnimationFrame\(step\)/);
  assert.doesNotMatch(source,/behavior:'auto'\);\s*return;\s*\}\s*window\.scrollTo\(0/);
  // Reduced motion still gets a direct jump rather than an animation.
  assert.match(source,/prefers-reduced-motion: reduce/);
});

test('following can be unlocked by the reader, not only by scrolling away',()=>{
  const hook=fs.readFileSync(path.join(__dirname,'../app/author/useProductionFollow.ts'),'utf8');
  const page=fs.readFileSync(path.join(__dirname,'../app/author/page.tsx'),'utf8');
  // The hook exposes an explicit unlock that also drops any easing already in flight, so releasing
  // the page cannot be undone by the animation that was running.
  assert.match(hook,/const unlock=useCallback\(\(\)=>\{/);
  assert.match(hook,/window\.cancelAnimationFrame\(frame\.current\);frame\.current=undefined;/);
  assert.match(hook,/state\.current\.following=false;/);
  assert.match(hook,/return \{targetRef,following,unlock,resume,ready:!!target\}/);
  // A scroll or drag also releases the easing at once, instead of being pulled back mid-gesture.
  assert.match(hook,/const markManual=\(\)=>\{\s*manualUntil\.current=performance\.now\(\)\+900;/);
  assert.match(hook,/if\(!force&&performance\.now\(\)<manualUntil\.current\)return;/);
  // While following, the control offers "解锁"; when paused, the existing resume button returns.
  assert.match(page,/className="studio-follow-unlock"[^>]*onClick=\{productionFollow\.unlock\}>解锁</);
  assert.match(page,/onClick=\{productionFollow\.resume\}/);
});
