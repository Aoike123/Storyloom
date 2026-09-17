type Image={task_id:string;name:string;category?:string;prompt?:string;needs_prompt_edit?:boolean};
type Props={
  images:Image[];paid:boolean;busy:boolean;
  onRetry:(id:string,prompt?:string)=>void;onRetryAll?:()=>void;
  drafts?:Record<string,string>;onDraftChange?:(id:string,text:string)=>void;
};

export default function ImageRecovery({images,paid,busy,onRetry,onRetryAll,drafts={},onDraftChange}:Props) {
  if(!images.length)return null;
  // A content-policy rejection repeats for the same text, so those pictures get an editor instead
  // of a plain retry; the rest are brought back with the prompts they already saved.
  const rejected=images.filter(image=>image.needs_prompt_edit);
  const plain=images.filter(image=>!image.needs_prompt_edit);
  return <section className="asset-redesign" aria-label="恢复失败图片">
    <h3>补齐失败图片</h3>
    <p>已完成的图片和设计会保留。重试将使用该图片已保存的提示词，再次调用生图服务。</p>
    {plain.length>1&&onRetryAll&&<div className="asset-redesign-heading"><strong>{plain.length} 张图片需要重试</strong><button type="button" className="button primary" disabled={busy||!paid} onClick={onRetryAll}>全部重试这 {plain.length} 张</button></div>}
    {plain.map(image=><div className="asset-redesign-heading" key={image.task_id}><strong>{image.name}</strong><button type="button" className="button primary" disabled={busy||!paid} onClick={()=>onRetry(image.task_id)}>重试这张图片</button></div>)}
    {rejected.map(image=>{
      const saved=(image.prompt||'').trim();
      const text=(drafts[image.task_id]??image.prompt??'').trim();
      const ready=text.length>=10&&text!==saved;
      const reason=busy?'正在提交，请稍候。':!paid?'勾选上方付费调用后可重试。'
        :text.length<10?'提示词至少需要 10 个字符。'
        :!ready?'请先修改提示词：同样的请求会被供应商再次拒绝。':'';
      return <div className="asset-redesign-heading is-rejected" key={image.task_id}>
        <div>
          <strong>{image.name}</strong>
          <p>供应商判定这条提示词要求的内容违规，没有返回画面。请改写后再提交，同样的问题会被再次拒绝。</p>
          <textarea aria-label={image.name+' 生图提示词'} rows={6} maxLength={6000} value={drafts[image.task_id]??image.prompt??''} onChange={event=>onDraftChange?.(image.task_id,event.target.value)}/>
          <small>{reason||'修改后的提示词不再经过结构化校验，生成结果仍需你逐张确认。'}</small>
        </div>
        <div className="asset-redesign-action">
          <button type="button" className="button primary" disabled={busy||!paid||!ready} title={reason||undefined} onClick={()=>onRetry(image.task_id,text)}>用修改后的提示词重试</button>
        </div>
      </div>;
    })}
    <small>{!paid?'勾选上方付费调用后可重试。':'图片补齐后会自动进入确认图片。'}</small>
  </section>;
}
