type Image={task_id:string;name:string};
export default function ImageRecovery({images,paid,busy,onRetry}:{images:Image[];paid:boolean;busy:boolean;onRetry:(id:string)=>void}) {
  if(!images.length)return null;
  return <section className="asset-redesign" aria-label="恢复失败图片">
    <h3>补齐失败图片</h3>
    <p>已完成的图片和设计会保留。重试将使用该图片已保存的提示词，再次调用生图服务。</p>
    {images.map(image=><div className="asset-redesign-heading" key={image.task_id}><strong>{image.name}</strong><button type="button" className="button primary" disabled={busy||!paid} onClick={()=>onRetry(image.task_id)}>重试这张图片</button></div>)}
    <small>{!paid?'勾选上方付费调用后可重试。':'图片补齐后会自动进入确认图片。'}</small>
  </section>;
}
