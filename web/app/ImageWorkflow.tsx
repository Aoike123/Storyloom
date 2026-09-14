'use client';
import {useState,useEffect} from 'react';
type Asset={motion_prompt?:string,id:string,name:string,media:string,status:string};
export default function ImageWorkflow({assets,enabled,onSaved}:{assets:Asset[],enabled:boolean,onSaved:()=>Promise<void>}){
 const [prompt,setPrompt]=useState('漫画风格，雨夜站台，短发女记者警惕地回头。中景，清晰的人物与场景。');
 const [motion,setMotion]=useState('镜头缓慢推进，人物转头看向镜头，保持人物外貌与场景一致。');
 const [selected,setSelected]=useState(''),[paid,setPaid]=useState(false),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
 const frames=assets.filter(a=>a.media&&/\.(png|jpe?g|webp)$/i.test(a.media));
 const frame=frames.find(a=>a.id===selected);
 useEffect(()=>{if(frame?.motion_prompt)setMotion(frame.motion_prompt);},[selected,frame?.motion_prompt]);
 async function send(path:string,body:unknown){setBusy(true);setMessage('');try{const r=await fetch('/api'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'请求失败');await onSaved();setMessage(path.startsWith('/review')?'图片已审核，可继续生成视频。':'任务已加入工作流记录，完成后可在下方选择图片或前往原版制作查看视频。');}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}}
 return <section className="config-guide"><h2>参考图 → 视频</h2><p>先生成参考图片，查看并审核，再交给 MiniMax 按图片参考模式生成视频。各步骤的实际用量在右下角面板累计显示。</p>
 <label>参考图画面描述<textarea aria-label="参考图画面描述" value={prompt} onChange={e=>setPrompt(e.target.value)}/></label>
 <label className="checkbox"><input type="checkbox" checked={paid} onChange={e=>setPaid(e.target.checked)}/>确认所点击的生成操作会调用收费模型</label>
 <button className="button primary" disabled={!enabled||!paid||busy} onClick={()=>send('/image',{prompt,confirm_paid:true})}>生成参考图</button>
 {!enabled&&<p>请先在上方保存生图 Key、接口和模型，并开启付费调用。</p>}
 <label style={{display:'block',marginTop:20}}>选择参考图 <select aria-label="选择参考图" value={selected} onChange={e=>setSelected(e.target.value)}><option value="">请选择生成或上传的图片</option>{frames.map(a=><option key={a.id} value={a.id}>{a.name} · {a.status==='approved'?'已审核':'待处理'}</option>)}</select></label>
 {frame&&<><img src={frame.media} alt={frame.name} style={{display:'block',maxWidth:'100%',maxHeight:340,margin:'16px 0'}}/>{frame.status!=='approved'&&<button className="button secondary" disabled={busy} onClick={()=>send('/review/'+frame.id,{status:'approved',note:'已查看并确认参考图'})}>确认图片可用</button>}</>}
 <label>镜头运动描述<textarea aria-label="镜头运动描述" value={motion} onChange={e=>setMotion(e.target.value)}/></label>
 <button className="button primary" disabled={!frame||frame.status!=='approved'||!paid||busy} onClick={()=>send('/video',{prompt:motion,asset_id:selected,confirm_paid:true})}>用该参考图生成视频</button><p role="status">{message}</p>
 </section>;
}
