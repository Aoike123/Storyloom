export type ProviderFailure={
  http_status?:number|null;category:string;summary:string;advice:string;
  provider_code?:string;provider_message?:string;request_id?:string;
  source:'response'|'legacy_status'|'not_submitted';
};

export default function ProviderError({error}:{error?:ProviderFailure|null}) {
  if(!error)return null;
  // A refusal raised before submission has no provider response to show: nothing was sent.
  const submitted=error.source!=='not_submitted';
  return <aside className="provider-error" aria-label="生图失败原因">
    <strong>{error.summary}{submitted?` · HTTP ${error.http_status}`:''}</strong>
    <p>{error.advice}</p>
    {error.provider_message&&<p className="provider-error-message">供应商说明：{error.provider_message}</p>}
    {error.source==='legacy_status'?<small>历史任务未保存原始响应，无法还原当次具体原因。下次请求会记录错误说明与请求编号。</small>
      :!submitted?<small>这次没有提交给供应商，因此没有供应商响应；换用可用的额度后可以直接重试这张图片。</small>
      :!error.provider_message&&<small>供应商未返回可展示的错误说明。</small>}
    {(error.provider_code||error.request_id)&&<details><summary>排查信息</summary>
      {error.provider_code&&<p>错误码：<code>{error.provider_code}</code></p>}
      {error.request_id&&<p>请求编号：<code>{error.request_id}</code></p>}
    </details>}
  </aside>;
}
