const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
function load(file){const context={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,context);return context.exports;}
const {TextReveal}=load('../app/textReveal.ts');

test('appended chunks continue from displayed text, without replaying earlier characters',()=>{
 const buffer=new TextReveal('先看人物的动作，再看镜头的变化。','one',true);
 buffer.update(buffer.target,'one',true,100);
 for(let now=116;now<=180;now+=16)buffer.advance(now);
 const shown=buffer.shown;
 assert.ok(shown.length>0&&shown.length<buffer.target.length);
 const next=buffer.target+'后一个镜头接住前一个动作。';
 buffer.update(next,'one',true,190);
 assert.equal(buffer.shown,shown);
 buffer.advance(220);
 assert.ok(buffer.shown.startsWith(shown));
 assert.ok(next.startsWith(buffer.shown));
});
test('large incoming batches catch up and completed output drains within 320ms',()=>{
 const text='画面保持一致。'.repeat(300);
 const buffer=new TextReveal(text,'one',true);
 buffer.update(text,'one',true,100);
 for(let now=116;now<2500;now+=16)buffer.advance(now);
 assert.equal(buffer.shown,text);
 buffer.update(text+'新段落。'.repeat(300),'one',true,2500);
 buffer.advance(2516);
 buffer.update(buffer.target,'one',false,2530);
 for(let now=2546;now<2860;now+=16){buffer.update(buffer.target,'one',false,now);buffer.advance(now);}
 buffer.advance(2860);
 assert.equal(buffer.shown,buffer.target);
});
test('historical completed text appears immediately and never gets replayed by polls',()=>{
 const buffer=new TextReveal('已完成的创作摘要','history',false);
 assert.equal(buffer.shown,buffer.target);
 buffer.update(buffer.target,'history',false,100);
 assert.equal(buffer.advance(116),false);
 buffer.update('已完成的创作摘要与检查结果','history',false,200);
 assert.equal(buffer.shown,buffer.target);
});
test('a new task never displays another task’s text',()=>{
 const buffer=new TextReveal('原作品的内容','old',false);
 buffer.update('新作品的内容','new',true,100);
 assert.equal(buffer.shown,'');
 buffer.advance(132);
 assert.ok('新作品的内容'.startsWith(buffer.shown));
 buffer.update('另一个历史作品','saved',false,200);
 assert.equal(buffer.shown,'另一个历史作品');
});
test('reduced motion and interrupted output flush immediately',()=>{
 const buffer=new TextReveal('正在收到的段落','one',true);
 buffer.update(buffer.target,'one',true,100,true);
 assert.equal(buffer.shown,buffer.target);
 buffer.update('保留的完整草稿','one',false,200,true);
 assert.equal(buffer.shown,'保留的完整草稿');
});
test('corrections keep a valid prefix and Unicode code points are never split',()=>{
 const buffer=new TextReveal('人物穿红衣','one',false);
 buffer.update('人物穿蓝衣🎨，再进入场景🌙','one',true,100);
 assert.equal(buffer.shown,'人物穿');
 for(let now=116;now<1500;now+=16){buffer.advance(now);assert.ok(buffer.target.startsWith(buffer.shown));assert.ok(!/[\uD800-\uDBFF]$/.test(buffer.shown));}
 assert.equal(buffer.shown,buffer.target);
});
