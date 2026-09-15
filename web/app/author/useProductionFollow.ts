'use client';

import {useCallback,useEffect,useRef,useState} from 'react';

const SCROLL_KEYS=new Set(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' ']);
// Following eases toward the box instead of jumping. The first version called scrollTo with
// behavior 'auto' on every content resize, so a typing answer moved the page in visible steps; one
// loop now chases the box while it grows.
const EASE=0.18;
const SETTLED=0.75;

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

/**
 * What to keep in view inside the registered module.
 *
 * The module wraps headings, tabs, the live text box and the finished-media rail, so scrolling it
 * as a whole wasted most of the viewport on chrome. Components mark the box that actually types —
 * the streaming region — and that inner box is followed instead. The last marked box wins, because
 * the later one in reading order is the content that keeps growing.
 */
export function followAnchor(container:HTMLElement){
  const marked=container.querySelectorAll<HTMLElement>('[data-follow="stream"]');
  return marked.length?marked[marked.length-1]:container;
}

function viewport(){
  const narrow=window.innerWidth<=760;
  const top=narrow?82:96, bottom=narrow?96:76;
  return {top,room:Math.max(180,window.innerHeight-top-bottom)};
}

/** Keep the live text in view without fighting deliberate reading/scrolling. */
export default function useProductionFollow({enabled,resetKey,targetKey}:{enabled:boolean;resetKey:string;targetKey:string}){
  const [target,setTarget]=useState<HTMLDivElement|null>(null);
  const [following,setFollowing]=useState(true);
  const node=useRef<HTMLDivElement|null>(null);
  const manualUntil=useRef(0);
  const frame=useRef<number|undefined>(undefined);
  // The animation loop runs outside React, so it reads the current flags from a ref instead of a
  // captured render value.
  const state=useRef({enabled,following:true});
  const targetRef=useCallback((next:HTMLDivElement|null)=>{
    node.current=next;
    setTarget(current=>current===next?current:next);
  },[]);

  useEffect(()=>{state.current.enabled=enabled;},[enabled]);
  useEffect(()=>{state.current.following=following;},[following]);

  /** Where the followed box should sit, and the scroll position that puts it there. */
  const destination=useCallback(()=>{
    const container=node.current;
    if(!container)return null;
    // Re-resolved on every step: the streaming box appears, moves between tasks and grows as the
    // page renders, so a cached element would go stale.
    const element=followAnchor(container);
    const rect=element.getBoundingClientRect();
    const {top,room}=viewport();
    const desired=rect.height<=room?top+(room-rect.height)/2:top;
    return {element,top:Math.max(0,window.scrollY+rect.top-desired)};
  },[]);

  const step=useCallback(()=>{
    frame.current=undefined;
    if(!state.current.enabled||!state.current.following)return;
    const found=destination();
    if(!found)return;
    const distance=found.top-window.scrollY;
    if(Math.abs(distance)<=SETTLED)return;
    if(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches){
      window.scrollTo({top:found.top,behavior:'auto'});
      return;
    }
    window.scrollTo(0,window.scrollY+distance*EASE);
    frame.current=window.requestAnimationFrame(step);
  },[destination]);

  const follow=useCallback((force=false)=>{
    if(!state.current.enabled||!state.current.following)return;
    // A gesture that just happened owns the scrollbar until it settles; the loop must not fight it.
    if(!force&&performance.now()<manualUntil.current)return;
    const found=destination();
    if(!found)return;
    // A box that is already comfortably visible is left alone; the page only moves for content that
    // would otherwise be pushed out of view.
    if(!force&&isEnoughVisible(found.element))return;
    if(frame.current===undefined)frame.current=window.requestAnimationFrame(step);
  },[destination,step]);

  useEffect(()=>{
    manualUntil.current=0;
    state.current.following=true;
    setFollowing(true);
  },[resetKey]);

  useEffect(()=>{
    if(!enabled||!target||!following)return;
    follow();
    const onResize=()=>follow(true);
    window.addEventListener('resize',onResize);
    let observer:ResizeObserver|undefined;
    if(typeof ResizeObserver!=='undefined'){
      observer=new ResizeObserver(()=>follow());
      observer.observe(target);
      const anchor=followAnchor(target);
      // The text box grows without the module's own box changing size, so both are watched.
      if(anchor!==target)observer.observe(anchor);
    }
    return()=>{
      window.removeEventListener('resize',onResize);
      observer?.disconnect();
    };
  },[enabled,target,targetKey,following,follow]);

  useEffect(()=>{
    if(!enabled||!target)return;
    let touchY:number|undefined;
    const markManual=()=>{
      manualUntil.current=performance.now()+900;
      // Drop any easing already in flight, so a scroll gesture is never pulled back mid-drag.
      if(frame.current!==undefined){window.cancelAnimationFrame(frame.current);frame.current=undefined;}
    };
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
      // Judged against the same box that is being followed, not the whole module.
      const visible=isEnoughVisible(followAnchor(target));
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
  /** Stop following at once, and drop any easing that is already in flight. */
  const unlock=useCallback(()=>{
    if(frame.current!==undefined){window.cancelAnimationFrame(frame.current);frame.current=undefined;}
    state.current.following=false;
    setFollowing(false);
  },[]);
  const resume=useCallback(()=>{
    manualUntil.current=0;
    state.current.following=true;
    setFollowing(true);
    follow(true);
  },[follow]);
  return {targetRef,following,unlock,resume,ready:!!target};
}
