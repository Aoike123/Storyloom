import json
import re
from urllib.parse import urlparse
import httpx
from .environment import model_config, env_file
from .model_access import (
    ModelAccessError,
    access_paid_states,
    authorize_call,
    mark_public_provider_unavailable,
    public_demo_mode,
)

class ProviderError(Exception):
    pass


class ModelOutputError(ProviderError):
    """The model request finished, but its response cannot satisfy the requested structure."""
    pass

def settings():
    cfg=model_config()
    paid=cfg.get('ALLOW_PAID_CALLS','false').lower()=='true'
    scoped=access_paid_states()
    if scoped is not None:paid=scoped['all']
    result={'llm_configured':all(cfg.get(k) for k in ['LLM_BASE_URL','LLM_MODEL','LLM_API_KEY']),
            'image_configured':cfg.get('IMAGE_PROVIDER')=='siliconflow' and all(cfg.get(k) for k in ['IMAGE_ENDPOINT','IMAGE_MODEL','IMAGE_API_KEY']),
            'image_model':cfg.get('IMAGE_MODEL',''),
            'video_configured':all(cfg.get(k) for k in ['VIDEO_ENDPOINT','VIDEO_MODEL','VIDEO_API_KEY']),
            'llm_model':cfg.get('LLM_MODEL',''), 'video_model':cfg.get('VIDEO_MODEL',''),
            'paid_enabled':paid,
            **{f'{kind}_paid_enabled':scoped[kind] if scoped is not None else paid for kind in ('llm','image','video')},
            'config_file':env_file().name,'llm_fast_model':cfg.get('LLM_FAST_MODEL') or cfg.get('LLM_MODEL',''),
            'image_adapter':'硅基流动文生图',
            'video_adapter':('MiniMax H3 V2' if cfg.get('VIDEO_PROVIDER','ark')=='minimax' else '火山方舟 Tasks（待真实验证）')}
    if not public_demo_mode():result['editable']={k:v for k,v in cfg.items() if not k.endswith('_API_KEY')}
    return result

def reserve_call(kind, task_id, model=None):
    cfg=settings()
    if not cfg.get(f'{kind}_paid_enabled',cfg['paid_enabled']): raise ProviderError('该模型的付费调用未开启或共享额度不足，请在模型连接页检查后再尝试。')
    if not cfg[f'{kind}_configured']: raise ProviderError('尚未配置该模型的完整 API 信息。')
    try:access=authorize_call(kind)
    except ModelAccessError as exc:raise ProviderError(str(exc)) from None
    from .provider_usage import begin
    return begin(kind,model or cfg.get(kind+'_model',''),task_id,access)


def endpoint(kind,config=None):
    cfg=config or model_config()
    value=cfg.get('LLM_BASE_URL','').rstrip('/')+'/chat/completions' if kind=='llm' else cfg.get(kind.upper()+'_ENDPOINT','')
    parsed=urlparse(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderError('真实模型接口必须使用不含账号、查询参数和片段的 HTTPS 地址。')
    return value


def chat_json(system,payload,task_id,profile='default',*,on_event=None):
    cfg=model_config()
    profiles={'default':('LLM_MODEL','LLM_MAX_TOKENS','LLM_TIMEOUT'),
              'fast':('LLM_FAST_MODEL','LLM_FAST_MAX_TOKENS','LLM_TIMEOUT')}
    if profile not in profiles:raise ProviderError('未知的文本模型用途。')
    model_key,token_key,timeout_key=profiles[profile]
    model=cfg.get(model_key) or cfg.get('LLM_MODEL')
    try:
        max_tokens=int(cfg[token_key]);timeout=float(cfg[timeout_key])
        if not 256<=max_tokens<=32768 or not 10<=timeout<=600:raise ValueError()
    except (ValueError,TypeError):
        raise ProviderError('文本模型的输出上限或超时配置无效。') from None
    target=endpoint('llm',cfg)
    activity=None
    if on_event is None:
        from .task_activity import model_activity
        activity=model_activity(task_id,payload,profile)
        if activity is not None:
            on_event=activity
            system+='\n先输出 display_summary，用两三句面向创作者的简短摘要说明本轮创作方向或检查重点，不输出内部推理步骤。然后输出原有字段。'
            if isinstance(payload.get('schema'),dict):
                schema=payload['schema']
                payload={**payload,'schema':{**schema,'properties':{'display_summary':{'type':'string','maxLength':1200},**schema.get('properties',{})}}}
    body={'model':model,'messages':[{'role':'system','content':system+'\n只输出 JSON。'},
          {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
          'response_format':{'type':'json_object'},'stream':on_event is not None,'max_tokens':max_tokens}
    if on_event is not None:
        body['stream_options']={'include_usage':True}
    if (model or '').startswith('deepseek'):
        body['thinking']={'type':'disabled'}
    entry=reserve_call('llm',task_id,model)
    from .provider_usage import finish
    try:
        request={'headers':{'Authorization':f'Bearer {cfg.get("LLM_API_KEY")}'},'json':body,'timeout':timeout,'follow_redirects':False}
        if on_event is not None:
            from .llm_stream import read_completion
            on_event('connecting','')
            with httpx.stream('POST',target,**request) as response:
                if response.status_code>=300:
                    finish(entry,status='rejected')
                    mark_public_provider_unavailable('llm',response.status_code)
                    raise ProviderError(f'语言模型返回 HTTP {response.status_code}；请核对模型权限与流式接口支持情况。')
                on_event('connected','')
                data=read_completion(response,on_event,timeout)
        else:
            response=httpx.post(target,**request)
            if response.status_code>=300:
                finish(entry,status='rejected')
                mark_public_provider_unavailable('llm',response.status_code)
                raise ProviderError(f'语言模型返回 HTTP {response.status_code}；请核对模型权限与接口格式。')
            data=response.json()
        finish(entry,data.get('usage',{}))
        if data['choices'][0].get('finish_reason')!='stop':
            raise ModelOutputError('语言模型输出未正常完成。')
        content=data['choices'][0]['message']['content']
        parsed=json.loads(content)
        if on_event is not None:on_event('validating',content)
        if activity is not None and isinstance(parsed,dict):parsed.pop('display_summary',None)
        return parsed,data.get('usage',{})
    except (httpx.TimeoutException,httpx.NetworkError,httpx.RemoteProtocolError):
        raise ProviderError('语言模型请求结果不确定，未自动重试；请核对供应商记录。') from None
    except (ValueError,KeyError,TypeError,IndexError):
        raise ModelOutputError('语言模型未返回符合约定的 JSON。') from None
def submit_video(prompt,task_id,image_url=None,*,local_frame=False,reference_images=None,duration_seconds=None,config=None):
    # Refresh configuration before constructing the request; never count local validation as a submission.
    cfg=config if config is not None else model_config()
    minimax=cfg.get('VIDEO_PROVIDER','ark')=='minimax'
    if reference_images is not None:
        if image_url or not isinstance(reference_images,list) or not 1<=len(reference_images)<=9:
            raise ProviderError('请提供 1–9 张参考图片，不能混用单图参数。')
        images=reference_images
    else:images=[image_url] if image_url else []
    if images and not minimax:raise ProviderError('当前图片参考视频流程需要 MiniMax H3 模型。')
    content=[{'type':'text','text':prompt}]
    for image in images:
        if not isinstance(image,str) or (urlparse(image).scheme!='https' and not (image.startswith('data:image/png;base64,') and (local_frame or reference_images is not None))):
            raise ProviderError('参考图需为 HTTPS 地址，或使用已审核的本地参考图片。')
        content.append({'type':'image_url','image_url':{'url':image},'role':'reference_image'})
    body={'model':cfg.get('VIDEO_MODEL'),'content':content}
    if minimax:
        model=body['model']
        if model not in ('MiniMax-H3','MiniMax-H3-Max'):
            raise ProviderError('MiniMax V2 模型应为 MiniMax-H3 或 MiniMax-H3-Max。')
        try: duration=int(duration_seconds if duration_seconds is not None else cfg.get('VIDEO_DURATION','8'))
        except ValueError: raise ProviderError('视频时长必须是整数秒。') from None
        resolution=cfg.get('VIDEO_RESOLUTION','768P')
        resolutions=('480P','768P') if model=='MiniMax-H3-Max' else ('768P','2K')
        if duration not in range(5 if model=='MiniMax-H3-Max' else 4,16) or resolution not in resolutions:
            raise ProviderError('视频时长或分辨率不受所选 MiniMax 模型支持。')
        body.update(duration=duration,resolution=resolution,ratio='16:9')
    if len(json.dumps(body,ensure_ascii=False).encode('utf-8'))>64*1024*1024:
        raise ProviderError('参考图片请求超过服务商 64MB 限制，请压缩参考图。')
    target=endpoint('video',cfg)
    entry=reserve_call('video',task_id)
    from .provider_usage import finish
    try:
        from .db import save_generation_request
        save_generation_request(task_id,model=body['model'],prompt=prompt,reference_count=len(images),input_mode='reference_images' if images else 'text')
        r=httpx.post(target,headers={'Authorization':f'Bearer {cfg.get("VIDEO_API_KEY")}'},
                     json=body,timeout=60)
        if r.status_code>=400:
            finish(entry,status='rejected')
            mark_public_provider_unavailable('video',r.status_code)
            raise ProviderError(f'视频接口返回 HTTP {r.status_code}；本任务不自动重新提交。')
        task=r.json().get('task_id' if minimax else 'id')
        if not task: raise ProviderError('视频接口未返回任务编号，请核对供应商记录。')
        return task
    except (httpx.TimeoutException,httpx.NetworkError):
        raise ProviderError('视频提交结果不确定，未重试。请到供应商控制台核实任务编号。') from None

def poll_video(provider_id):
    cfg=model_config()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',str(provider_id)):
        raise ProviderError('供应商任务编号格式无效。')
    minimax=cfg.get('VIDEO_PROVIDER','ark')=='minimax'
    target=endpoint('video',cfg).rstrip('/')+'/'+provider_id
    if minimax:
        configured=urlparse(endpoint('video',cfg))
        target=f'{configured.scheme}://{configured.netloc}/v2/query/video_generation/{provider_id}'
    try:
        r=httpx.get(target,
                    headers={'Authorization':f'Bearer {cfg.get("VIDEO_API_KEY")}'},timeout=30)
        if r.status_code>=400:
            mark_public_provider_unavailable('video',r.status_code)
            raise ProviderError(f'查询视频任务返回 HTTP {r.status_code}。')
        data=r.json()
        if minimax:
            task=data.get('task',{})
            return task.get('status','unknown'),task.get('content',{}).get('url')
        return data.get('status','unknown'),data.get('content',{}).get('video_url')
    except httpx.HTTPError: raise ProviderError('查询视频任务暂时失败，已保留厂商编号。') from None
