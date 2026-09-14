"""Persist display summaries separately from task results and paid-request state."""
import time
from sqlalchemy.orm import object_session
from .db import Session, Record, Task
from .style_progress import partial_json

MODEL_KINDS={'director','art_design','creative_revision','plan'}
CALL_TITLES={'Treatment':'梳理故事与改编方向','Board':'设计分镜与镜头衔接','Review':'检查故事与镜头连续性','AssetSheetPlan':'检查身份与服装绑定',
             'IdentityPlan':'锁定专业画风与唯一人物身份','WardrobeScenePlan':'设计独立服装与物理场景',
             'StylePlan':'专业画风参数','CharacterPlan':'锁定人物身份','CostumePlan':'设计独立服装','ScenePlan':'设计物理场景',
             'AssetPromptBatch':'编写资产生图提示词','ShotPromptBatch':'编写分镜图像与视频提示词',
             'CharacterSheet':'修改人物身份属性','CostumeSheet':'修改独立服装属性','SceneSheet':'修改物理场景属性',
             'ArtPlan':'设计人物与场景','Revision':'整理修改方向','Plan':'构思剧情变化'}


def clean(value,limit=1200):
    return ''.join(c for c in value[:limit] if not 0xD800<=ord(c)<=0xDFFF) if isinstance(value,str) else ''


def preview(content):
    parsed=partial_json(content)
    if not isinstance(parsed,dict):return {'summary':'','items':[]}
    summary=clean(parsed.get('display_summary'))
    items=[]
    if isinstance(parsed.get('visual_style'),dict):
        from .asset_sheets import style_prompt
        try:items.append({'title':'统一画风 · 专业参数','text':style_prompt(parsed['visual_style'])})
        except ValueError:pass
    for key,label in [('premise','故事设定'),('visual_strategy','画面方向'),
                      ('scope_note','改编范围'),('continuity','连续性检查'),('dramatic_logic','剧情逻辑'),
                      ('editability','剪辑衔接'),('production_feasibility','制作可行性')]:
        text=clean(parsed.get(key),1500)
        if text:items.append({'title':label,'text':text})
    for key,label,title_key,text_keys in [('rules','故事规则','rule',('consequence',)),
        ('characters','人物身份','name',()),('costumes','独立服装','name',()),('scenes','物理场景','name',()),('items','素材规格','name',()),('shots','镜头','id',('scene','dramatic_action','camera','first_frame','motion_prompt')),
        ('beats','剧情片段','title',('narration','reason'))]:
        values=parsed.get(key)
        if not isinstance(values,list):continue
        for index,item in enumerate(values[:18 if key in ('characters','costumes','scenes','items') else 8]):
            if not isinstance(item,dict):continue
            title=clean(item.get(title_key),180)
            parts=[clean(item.get('reference_prompt',item.get(field)) if field=='first_frame' else item.get(field),700) for field in text_keys]
            if key in ('characters','costumes','scenes','items'):
                from .asset_sheets import MODELS,description
                try:
                    if 'asset_index' in item and isinstance(item.get('prompt'),str):
                        title='素材 '+str(item['asset_index']+1);parts=[clean(item['prompt'],1500)]
                    else:parts=[description(MODELS[item['role']].model_validate(item))]
                except (KeyError,ValueError):parts=[]
            text='\n'.join(part for part in parts if part)
            if title or text:items.append({'title':label+' · '+(title or str(index+1)),'text':text})
    if not items and clean(parsed.get('prompt')):
        items.append({'title':'修改内容','text':clean(parsed['prompt'],1500)})
    if isinstance(parsed.get('issues'),list):
        for item in parsed['issues'][:5]:
            if isinstance(item,str):items.append({'title':'待核对的问题','text':clean(item)})
    return {'summary':summary,'items':items[:20]}


def activity_id(task_id):return 'activity_'+task_id


def activity_data(task):
    db=object_session(task)
    if db is not None:
        row=db.get(Record,activity_id(task.id))
        return dict(row.data) if row else None
    with Session() as db:
        row=db.get(Record,activity_id(task.id))
        return dict(row.data) if row else None


class Activity:
    def __init__(self,task_id,owner,title,kind='model',new_call=True):
        self.task_id=task_id;self.owner=owner;self.last_saved=0.0
        with Session() as db:
            previous=db.get(Record,activity_id(task_id))
            previous=dict(previous.data) if previous else {}
        now=time.time()
        if not new_call and previous:
            self.data=previous
            return
        history=previous.get('history',[])
        if previous.get('summary') or previous.get('items'):
            history=[*history,{key:previous.get(key) for key in ('title','summary','items','started_at','updated_at')}][-8:]
        self.data={'type':kind,'title':title,'phase':'preparing','started_at':previous.get('started_at',now),
                   'call_started_at':now,'updated_at':now,'summary':'','items':[],
                   'history':history,'events':previous.get('events',[])}

    def save(self,phase,message,display=None,force=False):
        from .providers import ProviderError
        now=time.time()
        display=display or {}
        changed=(phase!=self.data['phase'] or len(display.get('items',[]))>len(self.data.get('items',[]))
                 or bool(display.get('summary')) and not self.data.get('summary'))
        if not force and not changed and now-self.last_saved<.25:return
        events=self.data.get('events',[])
        if not events or events[-1]['message']!=message:
            events=[*events,{'phase':phase,'message':message,'at':now}][-24:]
        self.data={**self.data,**display,'phase':phase,'message':message,'updated_at':now,'events':events}
        with Session.begin() as db:
            task=db.get(Task,self.task_id)
            if not task or task.owner!=self.owner or task.status!='running':
                raise ProviderError('本次任务已停止，旧输出不再更新。')
            row=db.get(Record,activity_id(self.task_id))
            if row:row.data=dict(self.data);row.version+=1
            else:db.add(Record(id=activity_id(self.task_id),kind='task_activity',data=dict(self.data)))
            task.message=message
        self.last_saved=now

    def __call__(self,event,content=''):
        phase='drafting' if event=='delta' else event
        messages={'connecting':'正在连接模型，等待创作响应','connected':'模型已响应，等待创作内容',
            'reasoning':'模型正在构思，等待可展示的摘要','buffered':'模型一次性返回内容，正在检查',
            'validating':'内容已接收，正在核对完整方案','drafting':'创作摘要与方案正在展开'}
        self.save(phase,messages.get(phase,'正在准备创作'),preview(content) if content else None,force=event=='validating')


def model_activity(task_id,payload,profile):
    with Session() as db:
        task=db.get(Task,task_id)
        if not task or task.kind not in MODEL_KINDS or task.status!='running' or task.payload.get('mode')!='live':return None
        owner=task.owner
    schema=payload.get('schema') or {}
    title=CALL_TITLES.get(schema.get('title'),'检查剧情变化' if profile=='reader_review' else '构思创作方案')
    return Activity(task_id,owner,title)


def media_activity(task_id,owner,phase,message):
    with Session() as db:
        task=db.get(Task,task_id)
        title=task.payload.get('title') or ('生成画面' if task.kind=='image' else '生成视频')
    Activity(task_id,owner,clean(title,180),'media',new_call=False).save(phase,message,force=True)
