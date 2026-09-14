import math
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from .db import Session,Record,uid

router=APIRouter(prefix='/api/billing',tags=['billing'])

def rates(kind,model):
    with Session() as db:
        row=db.get(Record,'billing_rates')
        return (row.data if row else {}).get(kind+'|'+model,{})

def begin(kind,model,task):
    entry=uid('usage')
    price=rates(kind,model)
    with Session.begin() as db:
        db.add(Record(id=entry,kind='usage',data={'task':task,'provider':kind,'model':model,'submissions':1,
            'status':'pending','rates':price,'cost':None,'usage':{}}))
    return entry

def finish(entry,usage=None,status='completed'):
    if not entry:return
    with Session.begin() as db:
        row=db.get(Record,entry)
        if not row:return
        row.data={**row.data,'usage':usage or {},'status':status}

def finish_video(task,usage):
    with Session() as db:
        row=db.scalars(select(Record).where(Record.kind=='usage').order_by(Record.created.desc())).all()
        entry=next((r.id for r in row if r.data.get('task')==task and r.data.get('provider')=='video'),None)
    finish(entry,usage)

def number(value):
    return value if isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value) and value>=0 else None

def estimate(data):
    u=data.get('usage',{});p=data.get('rates',{});kind=data.get('provider')
    if data.get('status')!='completed':return None
    if kind=='llm':
        inp=number(u.get('prompt_tokens'));out=number(u.get('completion_tokens'))
        hit=number(u.get('prompt_cache_hit_tokens',0))
        if inp is None or out is None or hit is None or hit>inp:return None
        terms=[(inp-hit,'input'),(out,'output'),(hit,'cached_input')];scale=1000000
    elif kind=='image':terms=[(number(u.get('images')),'image')];scale=1
    elif kind=='video':terms=[(number(u.get('output_seconds',u.get('duration'))),'video_second')];scale=1
    else:return None
    if any(n is None or (n>0 and number(p.get(k)) is None) for n,k in terms):return None
    return sum(n*(p.get(k) or 0)/scale for n,k in terms)

@router.get('')
def summary():
    from .providers import settings
    from .reference_image_model import REFERENCE_IMAGE_MODEL
    cfg=settings();models={k:cfg.get(k+'_model','') for k in ('llm','image','video')}
    models['reference_image']=REFERENCE_IMAGE_MODEL
    with Session() as db:rows=list(db.scalars(select(Record).where(Record.kind=='usage')))
    tokens=0;images=0;seconds=0;cost=0;unknown=0;pending=0
    for row in rows:
        d=row.data;u=d.get('usage',{});c=estimate(d)
        if c is None:unknown+=1
        else:cost+=c
        if d.get('status')=='pending':pending+=1
        if d.get('provider')=='llm':
            tokens+=number(u.get('total_tokens')) or ((number(u.get('prompt_tokens')) or 0)+(number(u.get('completion_tokens')) or 0))
        elif d.get('provider')=='image':images+=number(u.get('images')) or 0
        elif d.get('provider')=='video':seconds+=number(u.get('output_seconds',u.get('duration'))) or 0
    return {'tokens':tokens,'images':images,'video_seconds':seconds,'estimated_cny':cost,'unpriced_calls':unknown,
            'pending_calls':pending,'submissions':len(rows),'models':models,'rates':{k:rates('image' if k=='reference_image' else k,m) for k,m in models.items()}}

class RateInput(BaseModel):
    kind:str
    model:str=Field(min_length=1,max_length=200)
    prices:dict[str,float]

@router.post('/rates')
def save_rates(body:RateInput):
    allowed={'llm':{'input','cached_input','output'},'image':{'image'},'video':{'video_second'}}
    if body.kind not in allowed or set(body.prices)-allowed[body.kind] or any(number(x) is None for x in body.prices.values()):
        raise HTTPException(422,'价格必须是有效的非负数。')
    with Session.begin() as db:
        row=db.get(Record,'billing_rates')
        key=body.kind+'|'+body.model
        if row:row.data={**row.data,key:body.prices}
        else:db.add(Record(id='billing_rates',kind='settings',data={key:body.prices}))
    return {'saved':True}
