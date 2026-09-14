'use client';
import {useEffect,useState} from 'react';
type Usage={tokens:number,images:number,video_seconds:number,estimated_cny:number,unpriced_calls:number,pending_calls:number,submissions:number,models:Record<string,string>,rates:Record<string,Record<string,number>>};
const priceFields:Record<string,Record<string,string>>={llm:{input:'输入 / 百万 token',cached_input:'缓存命中 / 百万 token',output:'输出 / 百万 token'},image:{image:'每张图片'},reference_image:{image:'每张参考合成图'},video:{video_second:'每秒视频（当前规格）'}};
export default function UsagePanel(){
 const [data,setData]=useState<Usage|null>(null),[expanded,setExpanded]=useState(true),[editing,setEditing]=useState(false),[draft,setDraft]=useState<Record<string,Record<string,string>>>({}),[message,setMessage]=useState(''),[offline,setOffline]=useState(false),[busy,setBusy]=useState(false);
 async function refresh(){const r=await fetch('/api/billing');if(!r.ok)throw new Error();setData(await r.json());setOffline(false);}
 useEffect(()=>{const load=()=>refresh().catch(()=>setOffline(true));load();const t=setInterval(load,3000);return()=>clearInterval(t);},[]);
 return <aside aria-label="用量与费用悬浮面板" style={{position:'fixed',right:18,bottom:18,zIndex:80,width:'min(340px,calc(100vw - 36px))',background:'#fff',color:'#182d37',border:'1px solid #cbd5d9',borderRadius:16,boxShadow:'0 8px 32px #18303d25',padding:16,maxHeight:'75vh',overflowY:'auto'}}>
 <div style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}><strong>用量 · 费用估算</strong><button aria-label={expanded?'收起用量面板':'展开用量面板'} onClick={()=>setExpanded(!expanded)}>{expanded?'收起':'展开'}</button></div>
 <p style={{margin:'10px 0',fontSize:20}}>{data?data.tokens.toLocaleString():'—'} <small>token</small> · {data&&data.submissions>data.unpriced_calls?'¥'+data.estimated_cny.toFixed(4):'待核算'}</p>
 {offline&&<p role="status">连接中断，当前显示上次取得的数据。</p>}
 {expanded&&<><p>{data?.images??'—'} 张图 · {data?data.video_seconds.toFixed(1):'—'} 秒视频</p><p>{data?.submissions??'—'} 次请求 · {data?.unpriced_calls??'—'} 次待核算 · {data?.pending_calls??'—'} 次等待用量</p><p style={{fontSize:12}}>仅累计已取得的用量，历史缺失数据未计入。文本用量来自供应商响应；视频按已保存素材时长估算。金额为已知部分的人民币估算，不是供应商账单。没有提交次数上限。</p>
 <button disabled={!data||busy} onClick={()=>{if(!data)return;setDraft(Object.fromEntries(Object.keys(priceFields).map(k=>[k,Object.fromEntries(Object.keys(priceFields[k]).map(f=>[f,data.rates[k]?.[f]===undefined?'':String(data.rates[k][f])]))])));setEditing(!editing);}}>设置计价单价</button>
 {editing&&data&&<form onSubmit={async e=>{e.preventDefault();setBusy(true);setMessage('');try{for(const kind of Object.keys(priceFields)){if(!data.models[kind])continue;const prices=Object.fromEntries(Object.entries(draft[kind]||{}).filter(([,v])=>v.trim()!=='').map(([k,v])=>[k,Number(v)]));const r=await fetch('/api/billing/rates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:kind==='reference_image'?'image':kind,model:data.models[kind],prices})});if(!r.ok)throw new Error('价格保存失败');}await refresh();setEditing(false);setMessage('已保存，仅对后续请求生效。');}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}}}>
 <p style={{fontSize:12}}>填写账号实际单价（人民币），留空表示未知；切换模型后分别设置。视频规格变化时请更新每秒单价。</p>
 {Object.entries(priceFields).map(([kind,fields])=><fieldset style={{marginTop:12,border:'1px solid #ddd'}} key={kind}><legend>{data.models[kind]||'模型未配置'}</legend>{Object.entries(fields).map(([key,label])=><label style={{display:'block',margin:'8px 0'}} key={key}>{label}<input aria-label={label} type="number" min="0" step="any" value={draft[kind]?.[key]??''} disabled={!data.models[kind]} onChange={e=>setDraft({...draft,[kind]:{...draft[kind],[key]:e.target.value}})} style={{width:'100%'}}/></label>)}</fieldset>)}<button disabled={busy}>保存单价</button></form>}
 {message&&<p role="status">{message}</p>}</>}
 </aside>;
}
