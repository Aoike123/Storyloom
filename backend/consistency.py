"""Approved project references bound to each shot, frozen by version."""
import hashlib
import json
from fastapi import APIRouter,HTTPException
from pydantic import AliasChoices,BaseModel,Field
from sqlalchemy import select
from .db import Record,Task,Session,record_dict
from .reference_image_model import REFERENCE_IMAGE_MODEL

router=APIRouter(prefix='/api/consistency',tags=['consistency'])

def config(db,pid):return db.get(Record,'visual_'+pid)

def stamp(db,row):
    refs=[]
    for aid in sorted(set(a for ids in row.data['bindings'].values() for a in ids)):
        a=db.get(Record,aid)
        refs.append((aid,a.version if a else None,a.data.get('status') if a else None))
    return hashlib.sha256(json.dumps([row.version,row.data,refs],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def shot_reference_ids(db,c,shot_id):
    """Resolve one shot's reviewed reference images exactly as they were bound and reviewed.

    Identity+costume pairs that the caller stitched with the local image tool stay one project
    reference; the request is never expanded past one request's image capacity here.
    """
    from .preproduction import MAX_REFERENCE_IMAGES
    ids=list(dict.fromkeys(c.data['bindings'].get(shot_id,[])))
    if len(ids)>MAX_REFERENCE_IMAGES:
        raise HTTPException(409,f'该镜需要 {len(ids)} 张参考图，超过视频模型每镜 {MAX_REFERENCE_IMAGES} 张的上限，请重新拆镜。')
    return ids


def ready(db,pid,sid=None,batch=False):
    c=config(db,pid)
    if not c or c.data.get('invalidated_at') or not c.data.get('approved'):raise HTTPException(409,'请先保存并审核视觉规范、参考资产与镜头绑定。')
    p=db.get(Record,pid)
    if not p or p.data.get('archived') or p.data.get('status')!='approved' or c.data['director_version']!=p.version:raise HTTPException(409,'分镜已更新，请重新审核资产绑定。')
    if p.data.get('requires_preproduction'):
        from .preproduction import ready as prep_ready
        if prep_ready(db,pid)['stamp']!=p.data.get('preproduction_stamp'):raise HTTPException(409,'选角搭景已变化，请重新制作分镜。')
    for aid in set(a for ids in c.data['bindings'].values() for a in ids):
        asset=db.get(Record,aid)
        if not asset or asset.kind!='asset' or asset.data.get('status')!='approved':raise HTTPException(409,'参考资产有未审核或失效的版本。')
        if c.data['asset_versions'].get(aid)!=asset.version:raise HTTPException(409,'参考资产已变更，请重新保存并审核视觉设定。')
    return c,stamp(db,c),True

@router.get('/{pid}')
def get_config(pid:str):
    with Session() as db:
        c=config(db,pid)
        p=db.get(Record,pid);prep=db.get(Record,'prep_'+pid)
        defaults=None
        if p and prep and p.data.get('requires_preproduction'):
            shots=p.data.get('board',{}).get('shots',[])
            defaults={'style':prep.data['style'],'bindings':{s['id']:s['assets'] for s in shots},'states':{s['id']:s['continuity_in']+' → '+s['continuity_out'] for s in shots},'approved':False}
        return {'defaults':defaults,'config':record_dict(c) if c else None,'stamp':stamp(db,c) if c else None,
            'assets':[record_dict(a) for a in db.scalars(select(Record).where(Record.kind=='asset')) if a.data.get('media')]}

class Visual(BaseModel):
    expected_version:int=0
    director_version:int
    style:str=Field(min_length=10,max_length=2000)
    bindings:dict[str,list[str]]
    states:dict[str,str]
    reference_model:str=Field(default=REFERENCE_IMAGE_MODEL,validation_alias=AliasChoices('reference_model','edit_model'))
    approved:bool=False

@router.post('/{pid}')
def save(pid:str,body:Visual):
    with Session.begin() as db:
        p=db.get(Record,pid)
        if not p or p.kind!='director':raise HTTPException(404,'导演方案不存在。')
        if p.data.get('archived'):raise HTTPException(409,'该方案已归档。')
        if p.version!=body.director_version:raise HTTPException(409,'导演版本已变化。')
        shots={s['id'] for s in p.data.get('board',{}).get('shots',[])}
        if set(body.bindings)!=shots or set(body.states)!=shots:raise HTTPException(422,'每镜都需要参考资产和连续性状态说明。')
        if body.reference_model!=REFERENCE_IMAGE_MODEL:raise HTTPException(422,'当前仅支持已验证的参考图协议。')
        if p.data.get('requires_preproduction'):
            from .preproduction import ready as prep_ready
            prep=prep_ready(db,pid)
            if body.style!=prep['style']:raise HTTPException(422,'画风已在选角搭景阶段确定，请返回前期准备修改。')
            if prep['stamp']!=p.data.get('preproduction_stamp'):raise HTTPException(409,'选角搭景已变化，请重新生成分镜。')
            for shot in p.data['board']['shots']:
                if not set(shot['assets'])<=set(body.bindings[shot['id']]) or not set(body.bindings[shot['id']])<=set(prep['assets']):raise HTTPException(422,'逐镜参考图必须包含分镜绑定的全部选角与影棚资产；超过三张时请拆镜。')
                from .asset_workflow import validate_shot_identities
                validate_shot_identities(body.bindings[shot['id']],prep['assets'])
        versions={}
        for sid,ids in body.bindings.items():
            from .preproduction import MAX_REFERENCE_IMAGES
            if not 1<=len(ids)<=MAX_REFERENCE_IMAGES or len(set(ids))!=len(ids):raise HTTPException(422,f'{sid} 必须绑定 1–{MAX_REFERENCE_IMAGES} 张不同参考图；超过上限请拆镜。')
            if not 2<=len(body.states[sid])<=1000:raise HTTPException(422,f'{sid} 需要说明服装、位置、道具及允许变化。')
            for aid in ids:
                a=db.get(Record,aid)
                if not a or a.kind!='asset' or a.data.get('status')!='approved':raise HTTPException(422,'参考图必须先在资产库审核。')
                from .asset_workflow import validate_asset_origin
                validate_asset_origin(db,a)
                versions[aid]=a.version
        c=config(db,pid)
        if (c.version if c else 0)!=body.expected_version:raise HTTPException(409,'视觉设定已更新，请刷新后修改。')
        data={**body.model_dump(exclude={'expected_version'}),'asset_versions':versions}
        if c and c.data.get('history'):data['history']=c.data['history']
        if c:c.data=data;c.version+=1
        else:c=Record(id='visual_'+pid,kind='visual_config',data=data);db.add(c)
        db.flush();return record_dict(c)
