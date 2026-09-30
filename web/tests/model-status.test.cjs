const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

const page=fs.readFileSync(path.join(__dirname,'../app/author/page.tsx'),'utf8');
const authorCss=fs.readFileSync(path.join(__dirname,'../app/author/author.css'),'utf8');

test('the workbench reads the local model status from the key-free endpoint',()=>{
  assert.match(page,/fetch\('\/api\/models'\)/);
  assert.match(page,/studio-models/);
  assert.match(page,/模型已就绪/);
  assert.match(page,/模型未配置/);
  assert.match(page,/.env\.local/);
});

test('the workbench shows no permission checkbox of any kind',()=>{
  assert.doesNotMatch(page,/许可/); // no permission wording at all (review-confirmation checkboxes are a different concept)
});

test('the status chips have their own stylesheet block',()=>{
  assert.match(authorCss,/\.(studio-models|studio-models\s)/);
});
