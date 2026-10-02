const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

// Every @keyframes in the app styles, classified. Motion added by the upgrade may only touch
// the compositor (opacity/transform), with one plan-sanctioned exception: hint-ring, the
// single one-shot box-shadow focus ring. Legacy keyframes keep their documented properties.
const NEW={
  'live-breathe':['opacity'],
  'check-in':['opacity','transform'],
  'hint-ring':['box-shadow'],
  'reader-enter':['opacity','transform'],
  'cover-halo':['opacity'],
  'cover-breathe':['transform'],
  'hero-form':['opacity','transform'],
  'story-import-fade':['opacity'],
  'card-enter':['opacity','transform'],
  'card-exit':['opacity'],
  'branch-check':['opacity'],
  'follow-flash':['opacity'],
  'asset-enter':['opacity'],
  'asset-exit':['opacity'],
  'publish-glow':['opacity'],
  'ui-enter':['opacity','transform'],
  'ambient-drift':['opacity','transform'],
  'folio-float':['transform'],
};
const LEGACY={
  'feedback-travel':['transform'],
  'feedback-turn':['transform'],
  'feedback-blink':['opacity'],
  'reader-skeleton':['opacity'],
  'reader-branch-pending':['background-position'],
  'studio-follow-pulse':['opacity','transform'],
};

function keyframesOf(css){
  const found={};
  for(const m of css.matchAll(/@keyframes\s+([A-Za-z-]+)\s*\{/g)){
    const name=m[1];
    let depth=0;
    const start=m.index+m[0].length-1;
    let j=start;
    while(j<css.length){
      if(css[j]==='{')depth++;
      else if(css[j]==='}'){depth--;if(depth===0)break;}
      j++;
    }
    const body=css.slice(start+1,j);
    const props=new Set();
    for(const b of body.matchAll(/(^|[;}{ ])\s*([a-zA-Z-]+)\s*:/g))props.add(b[2]);
    found[name]=[...props];
  }
  return found;
}

const app=path.join(__dirname,'..','app');
const files=[];
for(const dir of [app,path.join(app,'author')]){
  for(const name of fs.readdirSync(dir)){
    if(name.endsWith('.css'))files.push(path.join(dir,name));
  }
}
const all={};
for(const file of files){
  for(const [name,props] of Object.entries(keyframesOf(fs.readFileSync(file,'utf8')))){
    all[name]=props;
  }
}

test('every keyframe in the app styles is classified',()=>{
  const known=new Set([...Object.keys(NEW),...Object.keys(LEGACY)]);
  const unknown=Object.keys(all).filter(n=>!known.has(n));
  assert.deepEqual(unknown,[]);
  assert.deepEqual(Object.keys(all).length,24);
});

test('keyframes added by the upgrade animate only the compositor',()=>{
  for(const [name,props] of Object.entries(all)){
    if(!NEW[name])continue;
    const allowed=new Set(NEW[name]);
    for(const p of props){
      assert.ok(allowed.has(p),`@keyframes ${name} animates "${p}", which is not opacity/transform${name==='hint-ring'?' (hint-ring may use box-shadow)':''}`);
    }
  }
});

test('legacy keyframes stay within their documented properties',()=>{
  for(const [name,props] of Object.entries(all)){
    if(!LEGACY[name])continue;
    const allowed=new Set(LEGACY[name]);
    for(const p of props){
      assert.ok(allowed.has(p),`@keyframes ${name} now animates "${p}"; re-classify it before shipping`);
    }
  }
});

test('the plan-sanctioned layout exceptions are the only non-compositor transitions',()=>{
  const css=files.map(f=>fs.readFileSync(f,'utf8')).join('\n');
  // Import uses grid rows; native disclosures progressively enhance intrinsic height.
  // These local sizing transitions are the only exceptions to compositor motion.
  const legacyAllowed=new Set(['opacity','transform','translate','width','border-color','background','background-color','color','filter','box-shadow','grid-template-rows','scale','rotate','visibility','height','content-visibility']);
  const seen=new Set();
  for(const b of css.matchAll(/(?<!-)transition\s*:\s*([^;}]+)/g)){
    const parts=[];
    let depth=0,part='';
    for(const ch of b[1]){
      if(ch==='(')depth++;
      else if(ch===')')depth--;
      if(ch===','&&depth===0){parts.push(part);part='';}
      else part+=ch;
    }
    if(part)parts.push(part);
    for(const chunk of parts){
      const prop=chunk.trim().split(/\s+/)[0];
      if(!prop||prop==='none'||prop.endsWith('!important'))continue;
      seen.add(prop);
    }
  }
  const unexpected=[...seen].filter(p=>!legacyAllowed.has(p));
  assert.deepEqual(unexpected,[]);
});
