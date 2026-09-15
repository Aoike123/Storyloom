"""Image rejection diagnostics; never persist raw responses or credentials."""
import re
import time
from .db import Session, Record, Task


REJECTIONS={
    400:('invalid_request','生图参数未通过检查','请根据供应商说明检查模型及请求参数。'),
    401:('authentication','生图密钥验证失败','请在制作设置中检查生图 API Key。'),
    402:('balance','生图账户欠费或可用额度不足','请核对生图服务账户余额及可用额度，处理后重试这张图片。'),
    403:('permission','生图权限不足','请检查账户实名认证和模型使用权限。'),
    404:('not_found','生图模型或接口不存在','请检查生图接口地址和模型名称。'),
    429:('rate_limit','生图服务暂时限流','请按供应商说明等待后重试这张图片。'),
    451:('rejected','生图请求被服务商拒绝','请查看供应商说明；仅凭 HTTP 451 不能判定余额不足。'),
}
RETRYABLE_STATUS={400,401,402,403,404,409,422,429,451}
# A refusal raised before anything reached the provider. Nothing can have been billed, so the
# picture may be generated again; it is the one failure that carries no provider response.
NOT_SUBMITTED='not_submitted'


def safe_text(value,secrets=(),limit=800):
    if not isinstance(value,(str,int)) or isinstance(value,bool):return ''
    text=str(value)
    for secret in sorted((s for s in secrets if isinstance(s,str) and s),key=len,reverse=True):
        text=text.replace(secret,'[已隐藏]')
    text=re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+',r'\1 [已隐藏]',text)
    text=re.sub(r'\bsk-[A-Za-z0-9_-]+','[已隐藏]',text)
    text=re.sub(r'''(?i)(?:https?://|data:image/)[^\s<>"']+''','[链接已隐藏]',text)
    text=re.sub(r'''(?i)(api[_-]?key|access[_-]?token|secret|password|authorization)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''',r'\1=[已隐藏]',text)
    text=''.join(c for c in text if c in '\n\t' or c.isprintable())
    return text.strip()[:limit]


def describe(status):
    category,summary,advice=REJECTIONS.get(status,('service_error' if status>=500 else 'rejected',
        '生图服务暂时异常' if status>=500 else '生图请求未被接受',
        '请核对供应商记录后处理，未自动重复提交。'))
    return {'provider':'siliconflow','http_status':status,'category':category,'summary':summary,'advice':advice}


def refusal_error(reason):
    """A refusal raised before submission: the provider never saw this picture."""
    return {'provider':'local','http_status':None,'category':NOT_SUBMITTED,'summary':'这次生图没有提交给供应商',
            'advice':safe_text(reason) or '请确认这一步可以调用模型后重试这张图片。',
            'provider_message':'','provider_code':'','request_id':'','source':NOT_SUBMITTED,
            'captured_at':time.time()}


def note_refusal(task_id,reason):
    """Remember a refusal raised before submission, so the picture stays repairable.

    Every other failure carries a provider response to inspect, and a picture with no response and
    no record looked impossible to repair: a visitor who attached their own key after the operator's
    key ran out could only ever regenerate the one picture that had been refused by the provider.
    """
    error=refusal_error(reason)
    with Session.begin() as db:
        task=db.get(Task,task_id)
        if not task or task.kind!='image' or task.status!='running':return
        task.result={**task.result,'provider_error':error}
        activity=db.get(Record,'activity_'+task_id)
        if activity:
            event={'phase':'refused','message':error_message(error),'at':error['captured_at']}
            activity.data={**activity.data,'phase':'refused','message':event['message'],
                'updated_at':event['at'],'events':[*activity.data.get('events',[]),event][-24:]}


def response_error(response,config):
    error={**describe(response.status_code),'source':'response','captured_at':time.time()}
    secrets=[v for k,v in config.items() if re.search(r'KEY|TOKEN|SECRET|PASSWORD',k)]
    data={}
    if len(response.content)<=65536:
        try:data=response.json()
        except ValueError:pass
    if not isinstance(data,dict):data={}
    nested=data.get('error') if isinstance(data.get('error'),dict) else {}
    error['provider_code']=safe_text(nested.get('code',data.get('code')),secrets,80)
    error['provider_message']=safe_text(nested.get('message') or data.get('message') or data.get('detail') or data.get('error'),secrets)
    # Plain text is useful; HTML proxy/error pages may contain arbitrary request data.
    if not error['provider_message'] and not data and len(response.content)<=65536 and response.headers.get('content-type','').split(';')[0].strip()=='text/plain':
        error['provider_message']=safe_text(response.text,secrets)
    trace=next((response.headers[key] for key in ('x-siliconcloud-trace-id','x-request-id','request-id') if response.headers.get(key)),data.get('request_id',''))
    trace=safe_text(trace,secrets,160)
    error['request_id']=trace if re.fullmatch(r'[A-Za-z0-9._:-]{1,160}',trace) else ''
    return error


def error_message(error):
    if not error.get('http_status'):return f"{error['summary']}。{error['advice']}"
    return f"{error['summary']}（HTTP {error['http_status']}）。{error['advice']}"


def save_error(task_id,error):
    with Session.begin() as db:
        task=db.get(Task,task_id)
        if not task:return
        task.result={**task.result,'provider_error':error}
        activity=db.get(Record,'activity_'+task_id)
        if activity and task.status=='running':
            event={'phase':'rejected','message':error_message(error),'at':error['captured_at']}
            activity.data={**activity.data,'phase':'rejected','message':event['message'],
                'updated_at':event['at'],'events':[*activity.data.get('events',[]),event][-24:]}


def task_error(task):
    if task.kind!='image':return None
    if task.status in ('queued','running','waiting','completed'):return None
    saved=task.result.get('provider_error')
    if isinstance(saved,dict):return saved
    match=re.search(r'生图接口返回 HTTP (\d{3})',task.message or '')
    if match:
        return {**describe(int(match[1])),'source':'legacy_status','provider_message':'','provider_code':'','request_id':''}
    return None


def can_retry(task):
    """Whether one picture may be generated again without repeating a paid submission."""
    error=task_error(task)
    if not error or task.status not in ('failed','needs_review'):return False
    if task.result.get('asset_id') or task.result.get('media'):return False
    return error.get('source')==NOT_SUBMITTED or error.get('http_status') in RETRYABLE_STATUS
