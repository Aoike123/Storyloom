export type ProviderFailure={
  http_status:number;category:string;summary:string;advice:string;
  provider_code?:string;provider_message?:string;request_id?:string;
  source:'response'|'legacy_status';
};

export default function ProviderError({error}:{error?:ProviderFailure|null}) {
  if(!error)return null;
  return <aside className="provider-error" aria-label="生图失败原因">
    <strong>{error.summary} · HTTP {error.http_status}</strong>
    <p>{error.advice}</p>
    {error.provider_message&&<p className="provider-error-message">供应商说明：{error.provider_message}</p>}
    {error.source==='legacy_status'?<small>历史任务未保存原始响应，无法还原当次具体原因。下次请求会记录错误说明与请求编号。</small>:!error.provider_message&&<small>供应商未返回可展示的错误说明。</small>}
    {(error.provider_code||error.request_id)&&<details><summary>排查信息</summary>
      {error.provider_code&&<p>错误码：<code>{error.provider_code}</code></p>}
      {error.request_id&&<p>请求编号：<code>{error.request_id}</code></p>}
    </details>}
  </aside>;
}
