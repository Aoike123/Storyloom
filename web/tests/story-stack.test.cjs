const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');

const context={exports:{},require:name=>require(name)};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app/reader-types.ts'),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022},
}).outputText,context);
const {groupProductionsByStory, productionLink}=context.exports;

const release=(id, storyId, title)=>({id, source_work_id:storyId, title, source_title:'原作', author:'作者',
  description:'', entries:[{clip_id:'c', media:'/media/a.mp4', start:0, end:1, shot_id:'S01'}]});

// The helper runs inside its own vm realm, so its arrays have a different prototype; copy the ids
// into this realm before comparing.
const ids=productions=>Array.from(productions, p=>p.id);

test('one story made by several people becomes a single stacked card',()=>{
  const items=[{id:'story-1', work_id:'story-1', labels:[], title:'故事一', release:release('r1','story-1','版本甲')}];
  const releases=[release('r1','story-1','版本甲'),release('r2','story-1','版本乙'),release('r3','story-1','版本丙')];
  const groups=groupProductionsByStory(items,releases);
  assert.equal(groups.length,1,'one card for the story');
  assert.equal(groups[0].stacked,true);
  assert.deepEqual(ids(groups[0].productions),['r1','r2','r3'],'newest first, catalogue order kept');
});

test('a story with a single production is not marked as a stack',()=>{
  const items=[{id:'story-1', work_id:'story-1', labels:[], title:'故事一', release:release('r1','story-1','版本甲')}];
  const groups=groupProductionsByStory(items,[release('r1','story-1','版本甲')]);
  assert.equal(groups[0].stacked,false);
  assert.deepEqual(ids(groups[0].productions),['r1']);
});

test('a story nobody has made yet stays a plain card',()=>{
  const items=[{id:'story-2', work_id:'story-2', labels:[], title:'故事二', release:null}];
  const groups=groupProductionsByStory(items,[]);
  assert.equal(groups[0].stacked,false);
  assert.equal(groups[0].productions.length,0);
});

test('productions of different stories are never mixed',()=>{
  const items=[
    {id:'story-1', work_id:'story-1', labels:[], release:release('r1','story-1','甲')},
    {id:'story-2', work_id:'story-2', labels:[], release:release('r9','story-2','乙')},
  ];
  const releases=[release('r1','story-1','甲'),release('r2','story-1','甲二'),release('r9','story-2','乙')];
  const groups=groupProductionsByStory(items,releases);
  assert.deepEqual(ids(groups[0].productions),['r1','r2']);
  assert.deepEqual(ids(groups[1].productions),['r9']);
});

test('a release missing its story id falls back to the item own release',()=>{
  // Older releases may not carry source_work_id; the card must still show something watchable.
  const items=[{id:'story-3', work_id:'story-3', labels:[], release:release('r5','story-3','甲')}];
  const releases=[release('r5',undefined,'甲'),release('r6',undefined,'乙')];
  const groups=groupProductionsByStory(items,releases);
  assert.deepEqual(ids(groups[0].productions),['r5'],'grouped only by a real story id');
  assert.equal(groups[0].stacked,false);
});

test('entering a story from the shelf still targets the studio, not a model page',()=>{
  assert.equal(productionLink({work_id:'1747681485547843585'}),
    '/author?story=1747681485547843585');
});
