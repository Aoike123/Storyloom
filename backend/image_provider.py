import base64
import io
import os
from urllib.parse import urlparse
import httpx
from PIL import Image
from .db import DATA,save_generation_request
from .environment import model_config
from .providers import ProviderError,settings,reserve_call,endpoint
from .reference_image_model import REFERENCE_IMAGE_MODEL, REFERENCE_IMAGE_STEPS, MAX_REFERENCE_IMAGES


def _image_config():
    cfg=model_config()
    if cfg.get('IMAGE_PROVIDER')!='siliconflow':
        raise ProviderError('请选择硅基流动生图接口，其他协议尚未接入。')
    return cfg


def generate_image(prompt,task_id):
    """Create a new image from a complete text prompt."""
    cfg=_image_config()
    body={'model':cfg.get('IMAGE_MODEL'),'prompt':prompt,'image_size':'1024x1024'}
    return _request_image(body,task_id,cfg,input_mode='text_to_image')


def generate_from_references(prompt,task_id,*,references,model=REFERENCE_IMAGE_MODEL,steps=REFERENCE_IMAGE_STEPS):
    """Compose a production image from approved identity, wardrobe, or set references."""
    cfg=_image_config()
    if model!=REFERENCE_IMAGE_MODEL:
        raise ProviderError('当前参考图合成流程仅支持已验证的国内三图模型。')
    if not isinstance(steps,int) or not 1<=steps<=100:
        raise ProviderError('参考图合成质量参数必须是 1–100 的整数。')
    if not isinstance(references,(list,tuple)) or not 1<=len(references)<=MAX_REFERENCE_IMAGES:
        raise ProviderError(f'参考图合成需要 1–{MAX_REFERENCE_IMAGES} 张图片，不能丢弃多余参考图后继续。')
    body={'model':model,'prompt':prompt,'num_inference_steps':steps}
    for i,media in enumerate(references):body['image' if i==0 else f'image{i+1}']=local_frame_data(media)
    return _request_image(body,task_id,cfg,input_mode='reference_images',reference_count=len(references))


def _request_image(body,task_id,cfg,*,input_mode,reference_count=0):
    # Transport, accounting, and diagnostics are shared; generation/edit inputs are not.
    target=endpoint('image',cfg)
    entry=reserve_call('image',task_id,model=body['model']) if reference_count else reserve_call('image',task_id)
    from .billing import finish
    try:
        save_generation_request(task_id,model=body['model'],prompt=body['prompt'],image_size=body.get('image_size'),reference_count=reference_count,input_mode=input_mode,inference_steps=body.get('num_inference_steps'))
        r=httpx.post(target,headers={'Authorization':f'Bearer {cfg.get("IMAGE_API_KEY")}'},json=body,timeout=180)
        if r.status_code>=400:
            from .image_errors import response_error,save_error,error_message
            error=response_error(r,cfg)
            save_error(task_id,error)
            finish(entry,status='rejected')
            raise ProviderError(error_message(error))
        url=r.json()['images'][0]['url']
        if not isinstance(url,str) or urlparse(url).scheme!='https': raise ValueError()
        finish(entry,{'images':1})
        return url
    except httpx.HTTPError:
        raise ProviderError('生图请求结果不确定，未自动重试，请核对供应商记录。') from None
    except (ValueError,KeyError,IndexError,TypeError):
        raise ProviderError('生图接口未返回有效图片地址，未自动重试。') from None


def save_image(url,task_id):
    if urlparse(url).scheme!='https': raise ProviderError('图片下载地址必须使用 HTTPS。')
    raw=bytearray()
    try:
        with httpx.stream('GET',url,timeout=60,follow_redirects=False) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                raw.extend(chunk)
                if len(raw)>20*1024*1024: raise ProviderError('图片超过 20MB 限制。')
        with Image.open(io.BytesIO(raw)) as im:
            if im.width*im.height>16000000 or min(im.size)<256: raise ValueError()
            dest=DATA/'media'/f'{task_id}.png'
            im.convert('RGB').save(dest,format='PNG')
        return f'/media/{dest.name}'
    except (httpx.HTTPError,ValueError,OSError,Image.DecompressionBombError):
        raise ProviderError('图片下载或校验失败，未重新调用生图模型。') from None


def local_frame_data(media):
    root=(DATA/'media').resolve()
    if not isinstance(media,str) or not media.startswith('/media/'): raise ProviderError('参考图片不是本地素材。')
    path=(root/media.removeprefix('/media/')).resolve()
    if not path.is_relative_to(root) or path==root or not path.is_file(): raise ProviderError('参考图片文件不存在或路径无效。')
    if path.stat().st_size>20*1024*1024: raise ProviderError('参考图片超过 20MB 限制。')
    with Image.open(path) as im:
        if min(im.size)<256 or max(im.size)>5760 or not .4<=im.width/im.height<=2.5:
            raise ProviderError('参考图片尺寸不符合模型输入要求。')
        buf=io.BytesIO();im.convert('RGB').save(buf,format='PNG')
    raw=buf.getvalue()
    if len(raw)>30*1024*1024: raise ProviderError('转换后的参考图片超过 30MB。')
    return 'data:image/png;base64,'+base64.b64encode(raw).decode('ascii')
