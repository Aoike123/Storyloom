'use client';
import {useEffect,useRef,useState} from 'react';

type Layer={src:string;exit:boolean};

/**
 * One asset, one fixed box. A new picture fades in over 160ms; when a newer version replaces
 * the current one, the two crossfade over 220ms inside the same frame, so the grid around it
 * never moves while layers change.
 */
export default function AssetFrame({src,alt,video=false,controls=false}:{src:string;alt:string;video?:boolean;controls?:boolean}) {
  const [layers,setLayers]=useState<Layer[]>([{src,exit:false}]);
  const timer=useRef(0);
  useEffect(()=>{
    setLayers(current=>{
      if(current[0].src===src)return current;
      return [{src,exit:false},{src:current[0].src,exit:true}];
    });
    window.clearTimeout(timer.current);
    timer.current=window.setTimeout(()=>setLayers(current=>current.filter((_,i)=>i===0)),260);
  },[src]);
  useEffect(()=>()=>window.clearTimeout(timer.current),[]);
  const render=(layer:Layer,i:number)=>{
    const cls='asset-layer'+(layer.exit?' is-exiting':i===0?(layers.length>1?' is-entering':' is-first'):'');
    return video
      ? <video key={i} src={layer.src} className={cls} controls={controls&&!layer.exit} playsInline preload="metadata" aria-label={alt}/>
      : <img key={i} src={layer.src} alt={layer.exit?'':alt} className={cls} loading="lazy"/>;
  };
  return <div className={'asset-frame'+(video?' asset-frame-video':'')}>{[...layers].reverse().map(render)}</div>;
}
