'use client';

import {useCallback,useEffect,useRef,useState} from 'react';

const SCROLL_KEYS=new Set(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' ']);

function isEditable(target:EventTarget|null){
  const element=target instanceof HTMLElement?target:null;
  return !!element&&(element.isContentEditable||['INPUT','TEXTAREA','SELECT'].includes(element.tagName));
}

function isEnoughVisible(element:HTMLElement){
  const rect=element.getBoundingClientRect();
  if(rect.height<2)return false;
  const top=window.innerWidth<=760?78:88;
  const bottom=window.innerHeight-(window.innerWidth<=760?92:72);
  const visible=Math.max(0,Math.min(rect.bottom,bottom)-Math.max(rect.top,top));
  const required=Math.min(rect.height,Math.max(96,Math.min(240,(bottom-top)*.35)));
  return visible>=required;
}

/** Keep the live production module visible without fighting deliberate reading/scrolling. */
export default function useProductionFollow({enabled,resetKey,targetKey}:{enabled:boolean;resetKey:string;targetKey:string}){
  const [target,setTarget]=useState<HTMLDivElement|null>(null);
  const [following,setFollowing]=useState(true);
  const manualUntil=useRef(0);
  const frame=useRef<number|undefined>(undefined);
  const targetRef=useCallback((next:HTMLDivElement|null)=>setTarget(current=>current===next?current:next),[]);

  const scrollToTarget=useCallback((behavior:ScrollBehavior='smooth',force=false)=>{
    if(!target||(!force&&isEnoughVisible(target)))return;
    const rect=target.getBoundingClientRect();
    const topInset=window.innerWidth<=760?82:96;
    const bottomInset=window.innerWidth<=760?96:76;
    const room=Math.max(180,window.innerHeight-topInset-bottomInset);
    const desiredTop=rect.height<=room?topInset+(room-rect.height)/2:topInset;
    const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({top:Math.max(0,window.scrollY+rect.top-desiredTop),behavior:reduced?'auto':behavior});
  },[target]);

  const schedule=useCallback((behavior:ScrollBehavior='smooth',force=false)=>{
    if(frame.current!==undefined)window.cancelAnimationFrame(frame.current);
    frame.current=window.requestAnimationFrame(()=>{frame.current=undefined;scrollToTarget(behavior,force);});
  },[scrollToTarget]);

  useEffect(()=>{
    manualUntil.current=0;
    setFollowing(true);
  },[resetKey]);

  useEffect(()=>{
    if(!enabled||!target||!following)return;
    schedule('smooth');
    if(typeof ResizeObserver==='undefined')return;
    const observer=new ResizeObserver(()=>schedule('auto'));
    observer.observe(target);
    return()=>observer.disconnect();
  },[enabled,target,targetKey,following,schedule]);

  useEffect(()=>{
    if(!enabled||!target)return;
    let touchY:number|undefined;
    const markManual=()=>{manualUntil.current=performance.now()+900;};
    const onWheel=(event:WheelEvent)=>{if(Math.abs(event.deltaY)>2)markManual();};
    const onTouchStart=(event:TouchEvent)=>{touchY=event.touches[0]?.clientY;};
    const onTouchMove=(event:TouchEvent)=>{
      const next=event.touches[0]?.clientY;
      if(touchY!==undefined&&next!==undefined&&Math.abs(next-touchY)>6)markManual();
      touchY=next;
    };
    const onPointerDown=(event:PointerEvent)=>{
      // A scrollbar drag has no wheel/touch event, but starts at the viewport edge.
      if(event.pointerType==='mouse'&&event.clientX>=document.documentElement.clientWidth-18)markManual();
    };
    const onKeyDown=(event:KeyboardEvent)=>{
      if(SCROLL_KEYS.has(event.key)&&!isEditable(event.target))markManual();
    };
    const onScroll=()=>{
      if(performance.now()>manualUntil.current)return;
      const visible=isEnoughVisible(target);
      setFollowing(current=>current===visible?current:visible);
    };
    window.addEventListener('wheel',onWheel,{passive:true});
    window.addEventListener('touchstart',onTouchStart,{passive:true});
    window.addEventListener('touchmove',onTouchMove,{passive:true});
    window.addEventListener('pointerdown',onPointerDown,{passive:true});
    window.addEventListener('keydown',onKeyDown);
    window.addEventListener('scroll',onScroll,{passive:true});
    return()=>{
      window.removeEventListener('wheel',onWheel);
      window.removeEventListener('touchstart',onTouchStart);
      window.removeEventListener('touchmove',onTouchMove);
      window.removeEventListener('pointerdown',onPointerDown);
      window.removeEventListener('keydown',onKeyDown);
      window.removeEventListener('scroll',onScroll);
    };
  },[enabled,target]);

  useEffect(()=>()=>{if(frame.current!==undefined)window.cancelAnimationFrame(frame.current);},[]);
  const resume=useCallback(()=>{
    manualUntil.current=0;
    setFollowing(true);
    schedule('smooth',true);
  },[schedule]);
  return {targetRef,following,resume,ready:!!target};
}
