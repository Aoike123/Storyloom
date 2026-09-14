"""Hackathon public content API. Deliberately independent of account OAuth/CLI."""
import hashlib
import time
from threading import Lock
from urllib.parse import quote
import httpx
from fastapi import APIRouter,HTTPException
from .db import Session,Record,record_dict

router=APIRouter(prefix='/api/stories',tags=['stories'])
BASE='https://api.zhihu.com/km-indep-home/hackathon/v2/story/'
lock=Lock()
TTL=3600


def valid_id(value):
    return isinstance(value,str) and 0<len(value)<=120 and not any(c in value for c in '/?#\r\n') and not any(ord(c)<32 for c in value)


def fetch(path):
    try:
        r=httpx.get(BASE+path,headers={'Accept':'application/json'},timeout=30,follow_redirects=False)
        if r.status_code!=200: raise HTTPException(502,f'知乎故事接口返回 HTTP {r.status_code}，未自动重试。')
        if len(r.content)>8*1024*1024: raise HTTPException(502,'知乎响应超过本次读取上限。')
        return r.json()
    except httpx.HTTPError: raise HTTPException(502,'知乎故事接口连接失败，未自动重试。') from None
    except ValueError: raise HTTPException(502,'知乎故事接口未返回有效 JSON。') from None


def cached(key,path,refresh=False):
    with lock:
        with Session() as db:
            row=db.get(Record,key)
            previous=dict(row.data) if row else None
        if previous and not refresh and time.time()-previous['fetched_at']<TTL:
            return {**previous,'cached':True,'stale':False}
        try:
            raw=fetch(path)
            if path=='list':
                if not isinstance(raw,list) or any(not isinstance(x,dict) or not valid_id(x.get('work_id')) for x in raw):
                    raise HTTPException(502,'知乎故事列表格式不符合接口约定。')
            elif not isinstance(raw,dict): raise HTTPException(502,'知乎故事详情格式不符合接口约定。')
            elif raw.get('work_id') is not None and str(raw['work_id'])!=path:
                raise HTTPException(502,'知乎详情标识与所选故事不一致。')
        except HTTPException as exc:
            if previous:return {**previous,'cached':True,'stale':True,'warning':exc.detail}
            raise
        data={'raw':raw,'fetched_at':time.time(),'source_url':BASE+path}
        with Session.begin() as db:
            row=db.get(Record,key)
            if row:row.data=data;row.version+=1
            else:db.add(Record(id=key,kind='zhihu_cache',data=data))
        return {**data,'cached':False,'stale':False}


@router.get('')
def stories(refresh:bool=False):
    result=cached('zhihu_story_list','list',refresh)
    return {k:v for k,v in {**result,'items':result['raw']}.items() if k!='raw'}


@router.get('/imports')
def imports():
    from sqlalchemy import select
    with Session() as db:
        return [record_dict(r) for r in db.scalars(select(Record).where(Record.kind=='story_source'))]


@router.get('/{work_id}')
def detail(work_id:str,refresh:bool=False):
    if not valid_id(work_id):raise HTTPException(422,'故事标识格式无效。')
    listing=stories()
    selected=next((x for x in listing['items'] if x['work_id']==work_id),None)
    if selected is None:raise HTTPException(404,'请从知乎故事列表选择作品。')
    key='zhihu_detail_'+hashlib.sha256(work_id.encode()).hexdigest()[:32]
    result=cached(key,quote(work_id,safe=''),refresh)
    return {**result,'listing':selected,'completeness':'unknown'}


@router.post('/{work_id}/import')
def import_story(work_id:str):
    source=detail(work_id)
    raw=source['raw'];content=raw.get('content')
    if not isinstance(content,str) or not content.strip():raise HTTPException(422,'接口未提供有效正文，无法导入。')
    digest=hashlib.sha256(content.encode()).hexdigest()
    # Immutable source versions: refreshing upstream never rewrites an imported source.
    import hashlib as h
    sid='source_'+h.sha256((work_id+digest).encode()).hexdigest()[:32]
    data={'work_id':work_id,'title':raw.get('chapter_name') or source['listing'].get('title') or '',
          'author_name':raw.get('author_name'),'labels':raw.get('labels',source['listing'].get('labels',[])),
          'content':content,'source':'知乎黑客松故事 API','source_url':source['source_url'],
          'fetched_at':source['fetched_at'],'content_hash':digest,'completeness':'unknown',
          'status':'imported','raw':raw,'listing':source['listing']}
    with Session.begin() as db:
        row=db.get(Record,sid)
        if not row:
            row=Record(id=sid,kind='story_source',data=data);db.add(row);db.flush()
        return {'story':record_dict(row),'stale':source['stale'],'warning':source.get('warning')}
