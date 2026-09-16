'use client';
import {useCallback,useEffect,useState} from 'react';
import Link from 'next/link';
import {ArrowLeft,ArrowRight,BookOpen,Check,Clapperboard,LocateFixed,TriangleAlert} from 'lucide-react';
import {activeStatuses,problemStatuses,InteractionFeedback,TaskProgress,WorkProgress,workStages,ProgressTask} from '../ProgressFeedback';
import './author.css';
import StyleProgress from '../StyleProgress';
import StyleOptions from './StyleOptions';
import useProjectProgress from './useProjectProgress';
import {canRetryCurrentNode,hasActiveProgress,mergeWorkspace,shouldPollProgress,viewedAuthorStage,authorDisplayStage,authorStageIds} from './projectProgress';
import ProductionProgress from './ProductionProgress';
import ProductionActivity from './ProductionActivity';
import GenerationPrompt from '../GenerationPrompt';
import AssetRedesign from './AssetRedesign';
import AttemptHistory from './AttemptHistory';
import ImageRecovery from './ImageRecovery';
import SegmentReview from './SegmentReview';
import AssetFeedback from './AssetFeedback';
import useModelPermission from './useModelPermission';
import AccountDock from '../AccountDock';
import NodeSkillsPanel from '../NodeSkills';
import {clearModelAccess,modelAccessHeaders,readModelAccess} from '../model-access';
import {announceBeansProblem,isBeansProblem,onPayerChanged} from '../account-dock';
import {emptyZhihuStatus,readZhihuStatus,zhihuLoginLink,zhihuNotice,type ZhihuStatus} from '../zhihu-account';
import useProductionFollow from './useProductionFollow';
async function api(path:string,body?:unknown,signal?:AbortSignal){const headers=modelAccessHeaders();const r=await fetch('/api/author'+path,body===undefined?{headers,signal}:{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body),signal});const d=await r.json();if(r.status===401&&typeof window!=='undefined')clearModelAccess();const detail=typeof d.detail==='string'?d.detail:'暂时无法完成操作，请稍后再试。';if(!r.ok){if(isBeansProblem(detail))announceBeansProblem(detail);throw Error(detail);}return d;}
const stages:Record<string,string>={style:'先为这个故事，选择一种气质。',segments_review:'这段故事，切成几幕来讲？',preparing:'故事中的人物，正在走向画面。',assets_review:'这些形象，符合你的想象吗？',storyboarding:'这一段，拆成怎样的镜头？',rendering:'让分镜真正动起来。',episode_review:'这一集完成了，发布它，还是继续下一集？',film_review:'最后一次审片，让故事准备好登场。',published:'这段脑洞，已经有了画面。'};
export default function Author(){
 const [accessReady,setAccessReady]=useState(false);
 const [works,setWorks]=useState<any[]>([]),[selected,setSelected]=useState(''),[work,setWork]=useState<any>(null),[opening,setOpening]=useState(true);
 const [art,setArt]=useState(''),[tone,setTone]=useState(''),[confirmed,setConfirmed]=useState(false),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[messageError,setMessageError]=useState(false),[syncError,setSyncError]=useState(''),[updated,setUpdated]=useState('');
 const [zhihu,setZhihu]=useState<ZhihuStatus|null>(emptyZhihuStatus);
 const [zhihuFlag,setZhihuFlag]=useState<string|null>(null);
 const [paid,setPaid]=useModelPermission(selected);
 const [notes,setNotes]=useState<Record<string,string>>({}),[promptDrafts,setPromptDrafts]=useState<Record<string,string>>({}),[index,setIndex]=useState(0),[filter,setFilter]=useState('all'),[expanded,setExpanded]=useState(false);
 const [viewedStep,setViewedStep]=useState<string|null>(null);
 const [refresh,setRefresh]=useState(0),[actionLabel,setActionLabel]=useState('正在提交操作，等待服务确认…');
 const refreshWorkspace=useCallback(()=>setRefresh(n=>n+1),[]);
 const recommendation=work?.recommend_task_status;
 const progressActive=hasActiveProgress(work);
 const streamConnection=useProjectProgress(work,progressActive,setWork,refreshWorkspace);
 useEffect(()=>{
  let live=true;
  readZhihuStatus().then(status=>{if(live)setZhihu(status);}).catch(()=>{if(live)setZhihu({...emptyZhihuStatus});});
  return()=>{live=false;};
},[]);
 useEffect(()=>{
  // The visitor just attached their own keys. Work that stopped for lack of a payer can continue,
  // and the dock's bean count is no longer the relevant budget.
  return onPayerChanged(()=>{
   readZhihuStatus().then(status=>setZhihu(status)).catch(()=>undefined);
   setRefresh(n=>n+1);
  });
 },[]);
 useEffect(()=>{
  // The callback redirects back with `?zhihu=ok|error|denied`; show it once, then drop it from the
  // address so a reload does not repeat the message.
  const flag=new URLSearchParams(window.location.search).get('zhihu');
  if(flag)window.history.replaceState(null,'',window.location.pathname+(window.location.search.replace(/[?&]zhihu=[^&]*/,'').replace(/^&/,'?')));
  setZhihuFlag(flag);
 },[]);
 useEffect(()=>{
  // Entering a story signs the account in: without a payer, send the visitor straight to Zhihu and
  // come back to this exact page. A deployment without login configured just loads normally.
  if(!zhihu)return;
  // The server decides whether this browser can pay. Local storage is only a hint, so a leftover
  // token from a retired mode cannot skip the sign-in.
  if(!zhihu.can_generate&&zhihu.configured){
    window.location.replace(zhihuLoginLink(window.location.pathname+window.location.search));
    return;
  }
  setAccessReady(true);
},[zhihu]);
 useEffect(()=>{
  if(!accessReady)return;
  let live=true;const params=new URLSearchParams(window.location.search),id=params.get('work'),story=params.get('story');
  if(id){setSelected(id);setOpening(false);}
  else if(story){api('/stories/'+encodeURIComponent(story)+'/open',{}).then(d=>{if(live){setSelected(d.id);setWork(d);window.history.replaceState(null,'','/author?work='+encodeURIComponent(d.id));}}).catch(e=>{if(live){setMessage(e.message);setMessageError(true);}}).finally(()=>{if(live)setOpening(false);});}
  else setOpening(false);
  return()=>{live=false;};
 },[accessReady]);
 useEffect(()=>{if(!accessReady)return;let live=true;api('/projects').then(d=>{if(live){setWorks(d);const params=new URLSearchParams(window.location.search);if(!params.get('story')&&!params.get('work')&&d.length){setSelected(d[0].id);window.history.replaceState(null,'','/author?work='+encodeURIComponent(d[0].id));}}}).catch(e=>{if(live){setMessage(e.message);setMessageError(true);}});return()=>{live=false;};},[accessReady]);
 useEffect(()=>{
  if(!selected)return;
  let live=true,controller:AbortController|undefined;
  const load=async()=>{
   if(!live)return;
   controller=new AbortController();
   const signal=controller.signal;
   try{
    const d=await api('/projects/'+encodeURIComponent(selected),undefined,signal);
    if(!live||signal.aborted)return;
    setWork((current:any)=>mergeWorkspace(current,d));setSyncError('');setUpdated(new Date().toLocaleTimeString('zh-CN',{hour12:false}));
   }catch(e){
    if(live&&!signal.aborted)setSyncError((e as Error).message);
   }
  };
  void load();
  return()=>{live=false;controller?.abort();};
 },[selected,refresh]);
 useEffect(()=>{
  if(!selected||!shouldPollProgress(streamConnection,progressActive))return;
  const timer=window.setInterval(()=>{if(!document.hidden)refreshWorkspace();},30000);
  return()=>window.clearInterval(timer);
 },[selected,progressActive,streamConnection,refreshWorkspace]);
 useEffect(()=>{const read=()=>setViewedStep(new URLSearchParams(window.location.search).get('step'));read();window.addEventListener('popstate',read);return()=>window.removeEventListener('popstate',read);},[]);
 useEffect(()=>{if(work){setArt(work.art||'');setTone(work.tone||'');}},[work?.id,work?.run_id]);
 useEffect(()=>{setConfirmed(false);setIndex(0);},[work?.stage,work?.creative?.version,viewedStep,selected]);
 function showStep(next:string){
  if(viewedAuthorStage(actualStage,next)!==next||viewedAuthorStage(actualStage,viewedStep)===next)return;
  const url=new URL(window.location.href);
  if(next===actualStage){url.searchParams.delete('step');setViewedStep(null);}else{url.searchParams.set('step',next);setViewedStep(next);}
  window.history.pushState(window.history.state,'',url);
 }
 function followProduction(){setViewedStep(null);const url=new URL(window.location.href);url.searchParams.delete('step');window.history.replaceState(window.history.state,'',url);}
 async function restartPreparation(nextArt=work.art,nextTone=work.tone){await api('/projects/'+selected+'/start',{art:nextArt,tone:nextTone,confirm_paid:true,restart:work.stage!=='style',restart_from:viewedAuthorStage(actualStage,viewedStep)==='style'?'style':'preparing'});followProduction();}

 async function act(fn:()=>Promise<void>,label='正在提交操作，等待服务确认…'){setActionLabel(label);setBusy(true);setMessage('');setMessageError(false);try{await fn();setRefresh(n=>n+1);}catch(e){setMessage((e as Error).message);setMessageError(true);}finally{setBusy(false);}}
 const actualStage=authorDisplayStage(work),stage=viewedAuthorStage(actualStage,viewedStep),browsingEarlier=stage!==actualStage,source=work?.source,creative=work?.creative,shots=creative?.production?.shots||[];
 const jobs:ProgressTask[]=Array.from(new Map<string,ProgressTask>([...(work?.jobs||[]),work?.task,work?.recommend_task_status].filter(Boolean).map((t:ProgressTask)=>[t.id,t])).values());
 const active=jobs.filter(t=>activeStatuses.includes(t.status)),problems=jobs.filter(t=>problemStatuses.includes(t.status));
 const filtered=jobs.filter(t=>filter==='active'?activeStatuses.includes(t.status):filter==='attention'?problemStatuses.includes(t.status):true).slice().reverse();
 const cards=stage==='assets_review'?(creative?.items||[]).map((i:any)=>({name:i.name,description:i.design,task:i.task,asset:i.asset})):stage==='film_review'?shots.map((s:any)=>({name:s.shot.id+' · '+s.shot.dramatic_action,task:s.video_task,asset:s.clip})):[];
 const inFlight=[...(work?.jobs||[]),work?.task].some((t:ProgressTask|undefined)=>t&&activeStatuses.includes(t.status));
 const ready=cards.length>0&&cards.every((c:any)=>c.asset&&c.task?.status==='completed')&&!inFlight;
 const activeShot=shots[index],recommending=activeStatuses.includes(work?.recommend_task_status?.status),currentStage=workStages.find(s=>s.id===stage);
 const retryCurrentNode=!browsingEarlier&&canRetryCurrentNode(work,stage);
 // 重做在每个制作节点都可用：它不依赖是否报错，用来丢弃本轮记录重新开始。
 // 每个已经做完的步骤都能重做：在这一步重做，就会连带丢弃它之后的所有成果，
 // 所以在第一步点就是整枝重做，在最后一步点就是只重做本步。
 const redoableStages=['segments_review','preparing','assets_review','storyboarding','rendering','film_review','published'];
 const stagePosition=authorStageIds.indexOf(stage||''),actualPosition=authorStageIds.indexOf(actualStage||'');
 const canRedoStage=stagePosition>=0&&actualPosition>=0&&redoableStages.includes(stage||'')&&stagePosition<=actualPosition;
 const redoCascades=stagePosition<actualPosition;
 const reviewNotes=work?.storyboard_review;
 const reviewFlagged=!!reviewNotes&&(!reviewNotes.approved||(reviewNotes.issues||[]).length>0);
 const followEnabled=progressActive&&!browsingEarlier;
 const productionFollow=useProductionFollow({enabled:followEnabled,
  resetKey:selected+':'+(work?.run_id||''),
  targetKey:[stage,recommendation?.id||'',...active.map(task=>task.id+':'+task.status)].join('|')});
 return <main className="author-page">
  <header className="studio-header"><Link href="/" className="studio-brand">叙间<span>STORYLOOM / STUDIO</span></Link><div className="studio-header-right"><Link className="studio-back" href="/"><ArrowLeft size={15}/><span>返回故事市场</span></Link><AccountDock next={'/author'+(selected?'?work='+encodeURIComponent(selected):'')}/></div></header>
  <div className="studio-heading"><div><span className="studio-eyebrow">MICROFICTION TO MOTION</span><h1>把一个脑洞，拍成一幕。</h1><p>阅读原文，选择风格，见证微小说成为漫剧的每一步。</p></div>{works.length>0&&<label className="studio-picker">继续已有制作<select aria-label="继续已有制作" value={selected} onChange={e=>{if(e.target.value)window.location.href='/author?work='+encodeURIComponent(e.target.value);}}><option value="">选择我的制作</option>{works.map(w=><option value={w.id} key={w.id}>{w.title}</option>)}</select></label>}</div>
  <InteractionFeedback busy={opening||busy} text={opening?'正在读取所选微小说原文与制作记录…':busy?actionLabel:message} error={!opening&&!busy&&messageError}/>
  {syncError&&<div className="studio-error" role="alert">{syncError}{work?' · 当前显示上次同步结果。':''}<button disabled={busy} onClick={()=>setRefresh(n=>n+1)}>重新同步</button></div>}
  {zhihuFlag&&(()=>{const notice=zhihuNotice(zhihuFlag);return notice?<div className={notice.error?'studio-error':'studio-notice'} role="status">{notice.text}</div>:null;})()}
  {!opening&&!selected&&<section className="studio-empty"><BookOpen size={35}/><h2>从一篇原作开始</h2><p>前往故事市场选择一篇原作，阅读正文、确定画风，再把它制作成属于你的完整漫剧。</p><a className="button primary" href="/">去故事市场选原作 <ArrowRight size={15}/></a><WorkProgress/></section>}
  {!opening&&selected&&!work&&!syncError&&<InteractionFeedback busy text="正在同步这篇微小说的制作进度…"/>}
  {work&&<>
   <section className="studio-flow" aria-label="当前制作流程"><div className="studio-flow-heading"><div><span className="studio-eyebrow">PRODUCTION JOURNEY</span><h2>{work.title}</h2></div><span className={'studio-sync'+(syncError||!work.worker_online?' is-offline':'')}><i/>{syncError?'同步中断':work.worker_online?'后台制作服务在线':'后台制作服务离线'}<small>{updated&&'更新于 '+updated}</small></span></div><WorkProgress stage={actualStage} viewedStage={stage} onStepSelect={showStep} disabled={busy}/><p className="studio-note">已完成的步骤可点击返回查看和重新测试。</p></section>
   <NodeSkillsPanel/>
   {canRedoStage&&<section className={'studio-redo'+(redoCascades?' is-cascading':'')} aria-label="重做这一步">
     <div className="studio-redo-heading">
       <div>
       <strong>重做「{currentStage?.name||stage}」</strong>
       <p>{redoCascades
         ? '会删除这一步以及它之后所有步骤的成果，从这一步重新开始；原文、风格和更早的素材都会保留。'
         : '会删除这一步的成果，从这一步重新开始；后面的步骤还没有做，不受影响。'}</p>
       </div>
       <button type="button" className="button is-danger" disabled={busy||!paid}
         title={!paid?'请先勾选下方的模型调用许可':'删除这一步及其之后的成果，从这一步重新开始'}
         onClick={()=>act(async()=>{await api('/projects/'+selected+'/redo',{stage,confirm_paid:true});followProduction();},'正在删除这一步的成果并重新开始…')}>
         重做这一步{redoCascades?'（含之后）':''}
       </button>
     </div>
   </section>}
   <div className="studio-layout">
    <aside className="studio-source"><div className="studio-source-heading"><span><BookOpen size={16}/>微小说原文</span><small>{source?.labels?.join(' · ')||'脑洞'}</small></div><h2>{work.title}</h2>{source?.author_name&&<p className="studio-author">原著 / {source.author_name}</p>}{source?.title&&source.title!==work.title&&<p className="studio-chapter">{source.title}</p>}<div className="studio-source-body">{source?.content||'该历史作品未保存可展示的原文。'}</div><p className="studio-source-note">来源：{source?.source||'本地已保存作品'}。保留本次导入的原文版本。{source?.completeness==='unknown'?'接口未声明全文完整性，以上为实际返回正文。':''}</p>{work.source_warning&&<p className="studio-source-note">本次使用已缓存的原文，接口最新请求未成功。</p>}</aside>
    <div className="studio-main">
     {browsingEarlier&&<div className="studio-step-return" role="status">已返回「{currentStage?.name}」。实际制作进度：{workStages.find(s=>s.id===actualStage)?.name}。<button type="button" onClick={()=>showStep(actualStage)}>返回当前进度</button><p>{['storyboarding','rendering'].includes(stage||'')?'正在回看本节点保存的结果。当前失败节点处理完问题后可以继续，上游结果会复用。':'提交重新测试会从形象准备开始新一轮制作，原轮次的素材、分镜、视频及发布结果失效，保留在历史制作中。'}</p></div>}
     <section className="author-panel studio-action"><div className="studio-action-heading"><span className="studio-step">{String(workStages.findIndex(s=>s.id===stage)+1).padStart(2,'0')}</span><div><span className="studio-eyebrow">{currentStage?.name||'制作进度'}</span><h2>{stages[stage||'']||'查看当前制作状态'}</h2></div></div>
      {stage==='assets_review'&&<AssetRedesign paid={paid} busy={busy} running={inFlight} onPaidChange={setPaid} onRedesign={()=>act(async()=>{if(browsingEarlier){await restartPreparation();}else{await api('/projects/'+selected+'/redesign',{art:work.art,tone:work.tone,confirm_paid:true});followProduction();}setConfirmed(false);},'正在按设定图规范重新设计人物与场景…')}/>}
      {stage!=='style'&&stage!=='segments_review'&&<div ref={productionFollow.targetRef} className={'studio-live-module'+(followEnabled&&productionFollow.following?' is-following':'')}><ProductionProgress tasks={jobs} outputs={work.outputs||[]} stage={stage||actualStage} connection={streamConnection} phase={stage} node={(work.production_steps||[]).find((node:any)=>node.id===stage)}/></div>}
      {stage==='segments_review'&&(work.episodes||[]).length>0&&<SegmentReview episodes={work.episodes}>
        <div className="studio-confirm"><label className="checkbox"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>我已核对情节切割，确认按这个顺序制作</label>
          <button className="button primary" disabled={busy||!confirmed||!paid} onClick={()=>act(async()=>{await api('/projects/'+selected+'/segments/approve',{confirm:true});followProduction();},'正在确认情节切割，开始生成人物与场景…')}>确认情节，开始制作 <ArrowRight size={15}/></button></div>
      </SegmentReview>}
      {stage!=='segments_review'&&(work.episodes||[]).length>0&&<SegmentReview episodes={work.episodes} collapsed/>}
      {problems.length>0&&<div className="studio-error">有 {problems.length} 项任务需要处理，已生成的结果会保留。<button onClick={()=>{setFilter('attention');setExpanded(true);document.getElementById('studio-activity')?.scrollIntoView({behavior:'smooth'});}}>查看具体提示 <ArrowRight size={13}/></button>{retryCurrentNode&&<button disabled={busy||!paid} title={!paid?'请先勾选下方的模型调用许可':'带上结构错误重跑，保留已通过校验的片段'} onClick={()=>act(async()=>{await api('/projects/'+selected+'/retry-node',{confirm_paid:true});},'正在带上结构错误重新运行当前节点，保留已通过的片段…')}>重试本节点</button>}</div>}
      {stage!=='published'&&stage!=='assets_review'&&<label className="checkbox studio-paid"><input type="checkbox" checked={paid} onChange={e=>setPaid(e.target.checked)}/>允许本次操作调用付费模型；生成漫剧将继续执行后续制作步骤</label>}
      {stage==='style'&&<><p>让 AI 根据原文推荐几种电影视觉语言，也可以直接写下你的想法。</p><button className="button secondary" disabled={busy||recommending||!paid} onClick={()=>act(async()=>{const task=await api('/projects/'+selected+'/recommend',{confirm:true});setWork((current:any)=>current?.id===selected?{...current,recommend_task_status:task}:current);})}>{recommending?'AI 正在构思电影视觉…':'让 AI 推荐电影风格'}</button>{recommendation&&<div ref={productionFollow.targetRef} className={'studio-live-module'+(followEnabled&&productionFollow.following?' is-following':'')}><StyleProgress task={recommendation} detailed connection={streamConnection}/></div>}<StyleOptions task={recommendation} options={work.recommendations||[]} art={art} tone={tone} onSelect={(nextArt,nextTone)=>{setArt(nextArt);setTone(nextTone);}}/><label>电影视觉提示词<textarea rows={4} maxLength={500} value={art} onChange={e=>setArt(e.target.value)} placeholder="选用方案会带入全片视觉规则，也可以编辑成像媒介、构图、焦段、运动、灯光与调色。"/></label><label>剧情气质<input maxLength={300} value={tone} onChange={e=>setTone(e.target.value)} placeholder="例如：悬疑中带一些荒诞幽默"/></label><button className="button primary" disabled={busy||inFlight||!paid||art.trim().length<2||tone.trim().length<2} onClick={()=>act(async()=>{await restartPreparation(art,tone);},'正在保存电影视觉方向，安排人物与场景制作…')}>{browsingEarlier?'确定电影风格，重新测试后续步骤':'确定电影风格，生成人物与场景'} <ArrowRight size={15}/></button><p className="studio-note">本次会选取原文制作一个 4–8 镜头的完整短场景，人物与场景图片完成后由你确认。</p></>}
      {stage==='assets_review'&&<p>分别检查角色身份三视图（含人类、类人及神话生物）、需要的独立服装和无人场景。确认后直接用这些参考图编写组合分镜。</p>}
      {stage==='assets_review'&&(creative?.bare_costumes?.length||0)>0&&<div className="studio-note studio-bare-costumes"><strong>空衣服模式 · {creative.bare_costumes.length} 个角色</strong><ul>{(creative.bare_costumes||[]).map((item:any)=><li key={item.costume_id}>{item.name} · {item.costume_id} · {item.character_ref} — {item.description}</li>)}</ul><p className="studio-note">这些角色天然体表、不着衣物：服装环节已按空衣服模式给出结论，因此不会为它们生成服装图，画面里也不会添加衣物。角色 → 服装 → 场景的顺序对所有角色都完整跑过。</p></div>}
      {!browsingEarlier&&<ImageRecovery images={work.retryable_images||[]} paid={paid} busy={busy||inFlight} drafts={promptDrafts} onDraftChange={(id,text)=>setPromptDrafts(previous=>({...previous,[id]:text}))} onRetry={(id,prompt)=>act(async()=>{await api('/projects/'+selected+'/images/'+id+'/retry',{confirm_paid:true,...(prompt?{prompt}:{})});setPromptDrafts(previous=>{const next={...previous};delete next[id];return next;});setConfirmed(false);},prompt?'正在用修改后的提示词重新生成这张图片…':'正在恢复这张失败图片，保留其他已完成素材…')} onRetryAll={()=>act(async()=>{await api('/projects/'+selected+'/images/retry',{confirm_paid:true});setConfirmed(false);},'正在一次恢复全部失败图片，保留其他已完成素材…')}/>}
      {['preparing','storyboarding','rendering'].includes(stage||'')&&<><p>{browsingEarlier?'正在回看本步骤的任务、提示词和已有结果。':currentStage?.hint+'。可以离开页面，后台会继续执行并保存进度。'}</p>{!browsingEarlier&&!work.retryable_images?.length&&['failed','needs_review'].includes(work.task?.status)&&<button className="button secondary" disabled={busy} onClick={()=>act(async()=>{await api('/projects/'+selected+'/resume',{});})}>{['storyboarding','rendering'].includes(stage||'')?'继续当前节点（复用已保存结果）':'问题处理后继续制作'}</button>}</>}
      {stage==='assets_review'&&work.asset_review_skill&&<details className="studio-note"><summary>{work.asset_review_skill.title}</summary><ul>{(work.asset_review_skill.checklist||[]).map((item:string)=><li key={item}>{item}</li>)}</ul></details>}
      {['storyboarding','rendering','film_review','published'].includes(stage||'')&&(work.board_repairs?.changes?.length||0)>0&&<details className="studio-note studio-repairs"><summary>分镜参考经过 {work.board_repairs.changes.length} 处代码校正（模型原稿已被自动修复）</summary><ul>{(work.board_repairs.changes||[]).slice(0,20).map((change:any,index:number)=><li key={index}>{change.shot_id?change.shot_id+' · ':''}{change.action}{change.asset_id?' · '+change.asset_id:''}{change.source_asset_ids?' · '+change.source_asset_ids.join('、'):''}</li>)}</ul><p className="studio-note">这些是代码按确定映射补上或修正的绑定，不是模型自己写对的结果；请在看片时留意对应镜头。</p></details>}
      {['storyboarding','rendering','film_review','published'].includes(stage||'')&&reviewFlagged&&<aside className="studio-review-handoff" role="status"><header><span><TriangleAlert size={18}/></span><div><small>REVIEW NOTES</small><h3>文字预审提出的复核提示</h3></div></header><p>这是文字预审的创作意见，不是错误：它不影响制作，也不会自动返工。请在审片时重点检查。</p>{reviewNotes.issues?.length?<ul>{reviewNotes.issues.map((issue:string,index:number)=><li key={index}>{issue}</li>)}</ul>:<p>预审没有给出具体问题，请完整检查镜头衔接与叙事节奏。</p>}<small>如果与你的判断不同，可在上方「重试本节点」带上结构错误重跑，或用「重做本节点」删除本轮记录重新开始。</small></aside>}
      {['storyboarding','rendering','film_review','published'].includes(stage||'')&&work.storyboard_review&&<details className={'studio-note studio-review '+(work.storyboard_review.approved?'is-passed':'is-rejected')} open={!work.storyboard_review.approved}><summary>分镜专业预审 · {work.storyboard_review.approved?'通过':'有复核提示'}</summary><ul>{(work.storyboard_review.issues||[]).map((issue:string,index:number)=><li key={index}>{issue}</li>)}</ul>{!work.storyboard_review.issues?.length&&<p className="studio-note">预审没有给出具体问题。</p>}<p className="studio-note">这是文字预审，不是成片验收：连续性 {work.storyboard_review.continuity||'—'} · 戏剧逻辑 {work.storyboard_review.dramatic_logic||'—'} · 可剪辑性 {work.storyboard_review.editability||'—'} · 制作可行性 {work.storyboard_review.production_feasibility||'—'}</p></details>}
      {stage==='film_review'&&work.review_skill&&<aside className="studio-note"><strong>{work.review_skill.title}</strong><ul>{(work.review_skill.checklist||[]).map((item:string)=><li key={item}>{item}</li>)}</ul></aside>}
      {stage==='film_review'&&activeShot?.clip&&<div className="author-film"><h3>顺序观看 · {index+1} / {shots.length}</h3><video key={activeShot.clip.id} src={activeShot.clip.media} controls autoPlay={index>0} onEnded={()=>setIndex(i=>Math.min(i+1,shots.length-1))} onTimeUpdate={e=>{if(e.currentTarget.currentTime>=Math.min(activeShot.shot.edit_seconds,activeShot.clip.duration)){e.currentTarget.pause();if(index<shots.length-1)setIndex(index+1);}}}/><div>{shots.map((s:any,i:number)=><button className={'button '+(i===index?'primary':'secondary')} key={s.shot.id} onClick={()=>setIndex(i)}>{s.shot.id}</button>)}</div><p className="studio-note">按剪辑时长顺序播放视频。当前尚未提供独立配音、混音与单文件成片导出。</p></div>}
      {!browsingEarlier&&stage==='episode_review'&&<div className="studio-confirm"><p className="studio-note">本集已经生成完成：现在可以把它发布给读者，或者继续做下一幕。已经生成的部分会保留，未开始的情节也不会提前产生费用。</p><button className="button secondary" disabled={busy} onClick={()=>act(async()=>{await api('/projects/'+selected+'/publish',{confirm:true,confirm_paid:true});},'正在把已完成的情节发布到读者目录…')}>发布已完成的情节</button><button className="button primary" disabled={busy||!paid} onClick={()=>act(async()=>{await api('/projects/'+selected+'/episodes/continue',{confirm_paid:true});followProduction();},'正在开始下一幕的分镜与视频…')}>继续下一个情节 <ArrowRight size={14}/></button></div>}
      {!browsingEarlier&&['assets_review','film_review'].includes(stage||'')&&<div className="studio-confirm"><label className="checkbox"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>我已查看本轮{stage==='assets_review'?'人物与场景图片':'完整视频'}，确认满意</label><button className="button primary" disabled={busy||!confirmed||!ready||(stage==='assets_review'&&!paid)} onClick={()=>act(async()=>{await api('/projects/'+selected+(stage==='assets_review'?'/generate':'/publish'),{confirm:true,confirm_paid:true});},stage==='assets_review'?'正在确认图片，安排分镜与视频制作…':'正在核对片段，保存发布结果…')}>{stage==='assets_review'?'确认基础素材，生成分镜':'确认成片，发布到读者目录'} <ArrowRight size={15}/></button></div>}
      <div className="studio-asset-grid">{cards.map((c:any)=><article className="studio-asset" key={c.task?.id||c.name}><h3>{c.name}</h3>{c.description&&<p>{c.description}</p>}{c.asset?(stage==='film_review'?<video controls src={c.asset.media}/>:<img src={c.asset.media} alt={c.name}/>):<InteractionFeedback busy={activeStatuses.includes(c.task?.status)} text={c.task?.message||'等待制作'} title={stage==='film_review'?'视频制作':'画面制作'}/>}<GenerationPrompt generation={c.task?.generation} pending={activeStatuses.includes(c.task?.status)} group="asset-review-prompts"/><AssetFeedback name={c.name} task={c.task} text={notes[c.task?.id]||''} paid={paid} busy={busy} inFlight={inFlight} browsingEarlier={browsingEarlier} onTextChange={text=>setNotes(previous=>({...previous,[c.task?.id]:text}))} onPaidChange={setPaid} onSubmit={()=>act(async()=>{await api('/projects/'+selected+'/feedback',{task_id:c.task.id,text:notes[c.task.id],confirm_paid:true});setConfirmed(false);},'正在保存修改意见，安排重新制作…')}/></article>)}</div>
      {stage==='published'&&<div className="studio-published"><Clapperboard size={38}/><p>作品已进入读者目录，并前置展示为“已有漫剧”。</p><a className="button primary" href={work.release_id?'/?story='+encodeURIComponent(work.release_id):'/'}>观看这部漫剧 <ArrowRight size={15}/></a><a className="button secondary" href="/">选择下一个脑洞</a></div>}
     </section>
     <section className="author-panel studio-activity" id="studio-activity"><div className="studio-section-heading"><h2>制作动态</h2><span>{jobs.filter(t=>t.status==='completed').length} 已完成 · {active.length} 进行中 · {problems.length} 需处理</span></div><div className="studio-filters">{[['all','全部'],['active','进行中'],['attention','需处理']].map(([value,label])=><button key={value} className={filter===value?'is-selected':''} aria-pressed={filter===value} onClick={()=>{setFilter(value);setExpanded(false);}}>{label}</button>)}</div><ProductionActivity tasks={expanded?filtered:filtered.slice(0,5)}/>{!filtered.length&&<p className="studio-note">{filter==='attention'?'当前没有需要处理的任务。':filter==='active'?'当前没有正在执行的任务。':'确定风格并开始制作后，每一步的实际状态都会显示在这里。'}</p>}{filtered.length>5&&<button className="studio-expand" onClick={()=>setExpanded(!expanded)}>{expanded?'收起记录':'查看全部 '+filtered.length+' 项任务'}</button>}<p className="studio-note">创作摘要、制作阶段和已完成素材会持续更新；每项任务的过程记录可展开查看。</p></section>
     <AttemptHistory attempts={work.attempt_history||[]}/>
    </div>
   </div>
  </>}
  {followEnabled&&productionFollow.ready&&<div className={'studio-follow-control'+(productionFollow.following?' is-following':' is-paused')} aria-live="polite">{productionFollow.following?<div className="studio-follow-status"><i aria-hidden="true"/><span><strong>自动跟随制作</strong><small>{currentStage?.name||'当前节点'} · 滚动页面即暂停跟随</small></span></div>:<button type="button" onClick={productionFollow.resume}><LocateFixed size={18}/><span><strong>回到正在制作</strong><small>恢复自动跟随 · {currentStage?.name||'当前节点'}</small></span></button>}</div>}
  <footer className="studio-footer"><span>叙间 · 从一篇微小说，到一个可观看的故事</span></footer>
 </main>;
}
