'use client';
import {useCallback,useEffect,useState} from 'react';

const storageKey=(workId:string)=>'storyloom:paid:'+workId;
export function readModelPermission(workId:string) {
  try{return !!workId&&sessionStorage.getItem(storageKey(workId))==='true';}
  catch{return false;}
}
export function saveModelPermission(workId:string,allowed:boolean) {
  try{if(workId)sessionStorage.setItem(storageKey(workId),String(allowed));}
  catch{/* The current page can still use the explicit choice without storage. */}
}

export default function useModelPermission(workId:string) {
  const [choice,setChoice]=useState({workId:'',allowed:false});
  useEffect(()=>setChoice({workId,allowed:readModelPermission(workId)}),[workId]);
  const setAllowed=useCallback((allowed:boolean)=>{
    saveModelPermission(workId,allowed);setChoice({workId,allowed});
  },[workId]);
  return [choice.workId===workId&&choice.allowed,setAllowed] as const;
}
