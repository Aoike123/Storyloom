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
  assert.match(source,/const next=window\.scrollY\+distance\*EASE;/);
  assert.match(source,/window\.scrollTo\(0,next\)/);
  assert.match(source,/requestAnimationFrame\(step\)/);
  assert.doesNotMatch(source,/behavior:'auto'\);\s*return;\s*\}\s*window\.scrollTo\(0/);
  // Reduced motion still gets a direct jump rather than an animation.
  assert.match(source,/prefers-reduced-motion: reduce/);
});

test('the reader cancels following by scrolling, with no control to press',()=>{
  const hook=fs.readFileSync(path.join(__dirname,'../app/author/useProductionFollow.ts'),'utf8');
  const page=fs.readFileSync(path.join(__dirname,'../app/author/page.tsx'),'utf8');
  // A scroll we did not perform is the reader moving the page; that releases the easing at once,
  // so a scrollbar drag is never pulled back mid-gesture.
  assert.match(hook,/const ownScroll=useRef<number\|null>\(null\)/);
  assert.match(hook,/const ours=ownScroll\.current!==null&&Math\.abs\(window\.scrollY-ownScroll\.current\)<=OWN_SCROLL_TOLERANCE;/);
  assert.match(hook,/if\(!ours\)markManual\(\);/);
  assert.match(hook,/const markManual=\(\)=>\{\s*manualUntil\.current=performance\.now\(\)\+900;/);
  assert.match(hook,/if\(!force&&performance\.now\(\)<manualUntil\.current\)return;/);
  // Dragging the scrollbar starts at the viewport edge, and a wheel or trackpad produces ordinary
  // scroll events; both are covered without adding an unlock button to the interface.
  assert.match(hook,/event\.clientX>=document\.documentElement\.clientWidth-18/);
  assert.doesNotMatch(page,/studio-follow-unlock/);
  assert.match(page,/滚动页面即暂停跟随/);
  // When paused, the existing resume button returns.
  assert.match(page,/onClick=\{productionFollow\.resume\}/);
});
