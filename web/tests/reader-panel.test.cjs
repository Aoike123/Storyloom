const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');

function load(file){
  const context={exports:{},require:name=>{
    if(name.endsWith('.css')) return {};
    if(name==='./ReaderLink') return {default:props=>React.createElement('a',props)};
    if(name==='./ReaderArtwork') return {default:()=>React.createElement('span',{'data-artwork':true})};
    if(name==='./ReaderCreatorAvatar') return {default:()=>React.createElement('span',{'data-avatar':true})};
    if(name==='./reader-types') return {canWatch:()=>true,productionLink:()=>'/x',reducedMotion:()=>false};
    if(name==='./reader-carousel') return {adjacentId:()=>'',circularOffset:()=>0};
    return require(name);
  }};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app',file),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022},
  }).outputText,context);
  return context.exports;
}

const ReaderHero=load('ReaderHero.tsx').default;
const StoryImport=load('StoryImport.tsx').default;
const readerSource=fs.readFileSync(path.join(__dirname,'../app/ReaderExperience.tsx'),'utf8');
const heroProps=(over={})=>({items:[],loading:false,activeId:'',onActiveChange:()=>{},onOpen:()=>{},inviting:false,...over});

test('an empty market invites the first story import',()=>{
  const html=renderToStaticMarkup(React.createElement(ReaderHero,heroProps({inviting:true,onImport:()=>{}})));
  assert.match(html,/把你脑子里的微小说/);
  assert.match(html,/导入第一篇微小说/);
});

test('a stocked market keeps the browsing welcome without the import CTA',()=>{
  const html=renderToStaticMarkup(React.createElement(ReaderHero,heroProps({
    items:[{id:'s1',title:'故事一',description:'',labels:[],release:{id:'r1',entries:[{clip_id:'c',media:'/media/a.mp4',start:0,end:1}]},work_id:'s1'}],
  })));
  assert.match(html,/这段脑洞/);
  assert.doesNotMatch(html,/导入第一篇微小说/);
});

test('the import panel stays mounted so a draft survives closing and reopening',()=>{
  for(const open of [true,false]){
    const html=renderToStaticMarkup(React.createElement(StoryImport,{open,onImported:()=>{}}));
    assert.ok(html.includes('aria-label="导入故事"'),'panel form present while open='+open);
    assert.ok(html.includes('<textarea'),'textarea present while open='+open);
  }
});

test('the reader wires the panel with an inert clip and returns focus to the trigger',()=>{
  assert.match(readerSource,/story-import-slot/);
  assert.match(readerSource,/inert=\{!importOpen \|\| undefined\}/);
  assert.match(readerSource,/importBtnRef\.current\?\.focus\(\)/);
});
