const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');

const filename=path.join(__dirname,'../app/reader-carousel.ts');
const code=ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const context={exports:{},require:name=>name==='./reader-types'?{canWatch:item=>!!item.release?.entries?.length}:require(name)};
vm.runInNewContext(code,context);
const {adjacentId,circularOffset,productionRank,rankByProduction,wrapIndex}=context.exports;

test('the carousel wraps forever in both directions',()=>{
  assert.equal(wrapIndex(-1,3),2);
  assert.equal(wrapIndex(3,3),0);
  assert.deepEqual([0,1,2].map(index=>circularOffset(index,1,3)),[-1,0,1]);
  assert.equal(circularOffset(11,0,12),-1);
  assert.equal(circularOffset(0,11,12),1);
});

test('a swipe or arrow moves exactly one cover and wraps',()=>{
  const items=[{id:'a'},{id:'b'},{id:'c'}];
  assert.equal(adjacentId(items,0,1),'b');
  assert.equal(adjacentId(items,0,-1),'c');
  assert.equal(adjacentId(items,2,1),'a');
  assert.equal(adjacentId([{id:'only'}],0,1),'');
});

test('the most production-complete work is loaded first',()=>{
  const items=[
    {id:'new',stage:undefined,release:null},
    {id:'images',stage:'assets_review',release:null},
    {id:'review',stage:'film_review',release:null},
    {id:'ready',stage:'published',release:{entries:[{media:'/media/ready.mp4'}]}},
  ];
  assert.ok(productionRank(items[3])>productionRank(items[2]));
  assert.deepEqual(Array.from(rankByProduction(items),item=>item.id),['ready','review','images','new']);
});

test('equal production stages keep the catalogue order stable',()=>{
  const items=[{id:'first',stage:'style',release:null},{id:'second',stage:'style',release:null}];
  assert.deepEqual(Array.from(rankByProduction(items),item=>item.id),['first','second']);
});
