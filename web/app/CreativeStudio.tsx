'use client';
import {useState,useEffect} from 'react';
type Job={id:string,status:string,message:string};
type Media={id:string,media:string,status?:string};
type Card={name:string,task:Job|null,asset:Media|null,isVideo?:boolean,facts?:string};
const labels:Record<string,string>={designing:'正在理解原著并设计人物与场景',assets_review:'请审核人物与场景',trials_review:'请审核人物进入场景后的效果',storyboarding:'自动分镜与专业预审',samples_review:'请检查三镜连续画面',frames_review:'请确认本轮全部画面',videos_review:'视频制作与成片审核',published:'作品已发布'};
export default function CreativeStudio({projectId}:{projectId:string}){
 const [data,setData]=useState<any>(undefined),[art,setArt]=useState('温暖手绘动画，柔和水彩背景'),[tone,setTone]=useState('温馨风'),[paid,setPaid]=useState(false),[reviewed,setReviewed]=useState(false),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[feedback,setFeedback]=useState<Record<string,string>>({});
 const base='/api/creative/'+projectId;
 async function request(path:string,body?:unknown){const r=await fetch(path,body===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'请检查输入');return d;}
 useEffect(()=>{let active=true;const load=()=>request(base).then(d=>{if(active)setData(d);}).catch(e=>{if(active)setMessage(e.message);});load();const t=setInterval(load,2500);return()=>{active=false;clearInterval(t);};},[base]);
 useEffect(()=>setReviewed(false),[data?.stage]);
 async function act(fn:()=>Promise<void>){setBusy(true);setMessage('');try{await fn();setData(await request(base));}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}}
 const stage=data?.stage;let cards:Card[]=[];
 if(stage==='assets_review')cards=(data.items||[]).map((i:any)=>({name:i.name,task:i.task,asset:i.asset,facts:i.facts}));
 if(stage==='trials_review')cards=(data.trials||[]).map((i:any)=>({name:'场景试拍',task:i.task,asset:i.asset}));
 if(['samples_review','frames_review','videos_review','published'].includes(stage))cards=(data.production?.shots||[]).filter((s:any)=>stage!=='samples_review'||s.image_task).map((s:any)=>({name:s.shot.id+' · '+s.shot.dramatic_action,task:stage==='videos_review'||stage==='published'?s.video_task:s.image_task,asset:stage==='videos_review'||stage==='published'?s.clip:s.asset,isVideo:stage==='videos_review'||stage==='published'}));
 const inFlight=cards.some(c=>['queued','running','waiting'].includes(c.task?.status||''))||['queued','running','waiting'].includes(data?.task?.status);
 const waiting=cards.some(c=>!c.asset||c.task?.status!=='completed')||['queued','running','waiting'].includes(data?.task?.status);
 return <section className="creative-studio"><header><span className="eyebrow">你定方向，系统完成制作</span><h2>{labels[stage]||'想让这个故事呈现什么感觉？'}</h2><p>选择画风和剧情氛围即可。角色以原著为依据；你只需看图片、提修改意见，满意后继续。</p></header>
 {!data&&<><label>画风<input className="text-input" value={art} onChange={e=>setArt(e.target.value)} placeholder="描述喜欢的画风、参考作品或视觉特征"/></label><div className="harness-actions">{['温暖手绘动画，柔和水彩背景','黑白恐怖漫画，密集排线与诡异细节','清新日系漫画','写实电影质感'].map(v=><button className="button secondary" key={v} onClick={()=>setArt(v)}>{v}</button>)}</div><label>剧情风格<select value={tone} onChange={e=>setTone(e.target.value)}>{['阴暗风','搞笑风','温馨风','恐怖风','悬疑风','热血风'].map(v=><option key={v}>{v}</option>)}</select></label><p>原文没有说明的外观会作为改编设计补全，生成后可继续修改。</p></>}
 {data&&<p className="harness-notice">画风：{data.art} · 剧情氛围：{data.tone}</p>}
 <label className="checkbox"><input type="checkbox" checked={paid} onChange={e=>setPaid(e.target.checked)}/>确认本次点击所启动的模型制作费用（用量在悬浮面板显示）</label>
 {!data&&<button className="button primary" disabled={busy||!paid||art.trim().length<2} onClick={()=>act(async()=>{await request(base+'/design',{art,tone,confirm_paid:true});})}>按原著生成人物与场景</button>}
 {data?.task&&<p role="status">{data.task.message}</p>}
 <div className="production-grid">{cards.map((c,i)=><article className="production-shot" key={c.task?.id||i}><h3>{c.name}</h3>{c.facts&&<p>根据原著提取：{c.facts}</p>}<div className="production-preview">{c.asset?(c.isVideo?<video src={c.asset.media} controls preload="metadata"/>:<img src={c.asset.media} alt={c.name}/>):<div className="working-placeholder">{c.task?.message||'等待制作'}</div>}</div><p>{c.task?.message}</p>{stage!=='published'&&(c.asset||['failed','needs_review'].includes(c.task?.status||''))&&<><textarea aria-label={c.name+' 修改意见'} value={feedback[c.task!.id]||''} onChange={e=>setFeedback({...feedback,[c.task!.id]:e.target.value})} placeholder={c.isVideo?'例如：动作太快，放慢转身；镜头不要一直推进。':'例如：头发更短一些；衣服按原文改为灰色；房间更温暖。'}/><button className="button secondary" disabled={busy||inFlight||!paid||(feedback[c.task!.id]||'').trim().length<2} onClick={()=>act(async()=>{await request(base+'/feedback',{task_id:c.task!.id,text:feedback[c.task!.id],confirm_paid:true});setReviewed(false);setMessage('修改意见已交给系统，旧版本保留，新版本完成后请再次审核。');})}>按意见自动修改</button></>}</article>)}</div>
 {cards.length>0&&stage!=='published'&&<label className="checkbox"><input type="checkbox" checked={reviewed} onChange={e=>setReviewed(e.target.checked)}/>我已查看本轮全部{stage==='videos_review'?'视频':'图片'}，确认满意</label>}
 {['assets_review','trials_review','samples_review','frames_review'].includes(stage)&&<button className="button primary" disabled={busy||waiting||!paid||!reviewed||!cards.length} onClick={()=>act(async()=>{await request(base+'/continue',{stage,confirm_review:true,confirm_paid:true});})}>{{assets_review:'图片满意，自动组合场景',trials_review:'场景满意，自动分镜并制作预览',samples_review:'预览满意，自动完成其余画面',frames_review:'全部图片满意，自动制作视频'}[stage as string]}</button>}
 {stage==='videos_review'&&<button className="button primary" disabled={busy||waiting||!reviewed||!cards.length} onClick={()=>act(async()=>{await request(base+'/publish',{confirm:true});})}>成片满意，发布作品</button>}
 <p className="muted">系统负责资产绑定、空间与动作连续性、分镜预审及生成参数。画面质量由你最终确认；发现问题只需描述修改意见。原版发布前始终保留审核关卡。</p>{message&&<p role="status" className="harness-notice">{message}</p>}</section>;
}
