"""Small, explicit live benchmark. Records each paid submission before sending; reruns never resubmit it."""
import argparse,asyncio,base64,io,json,re,sys,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from dotenv import dotenv_values
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.billing import begin,finish
from backend.db import init_db
CFG={}
for filename in ('.env.local','.env'):
    if (ROOT/filename).exists():CFG.update(dotenv_values(ROOT/filename))
BASE='https://api.openai-next.com/v1'
PROMPT='A clean hand-drawn comic panel: an adult courier in a yellow raincoat stands beside a green postbox on a quiet street after rain. A tiny glowing paper crane rests on the postbox. Warm window light, blue evening sky, clear silhouettes, coherent anatomy, no text, no logos.'
VIDEO_PROMPT=PROMPT+' One continuous shot: the paper crane gently unfolds its wings and floats upward, the courier follows it with their eyes, slow subtle camera push-in, no cuts.'
STORY='雨停后，成年邮递员林舟发现绿色邮筒里有一只发光的纸鹤。纸鹤写着明天的日期。他把它放回邮筒，纸鹤却飞到街角的一扇暖黄色窗前。窗里的老人认出了自己年轻时的字迹。'
TASK='为以下微小说设计三种不同的漫剧视觉风格。只返回 JSON，结构为 {"options":[{"art":"画风，12字内","tone":"气质，12字内","reason":"适配原因，30字内"}]}。options 必须恰好三项，保留邮递员、绿色邮筒、发光纸鹤和老人这些事实。原文：'+STORY

def safe(value):
    text=str(value)
    for k,v in CFG.items():
        if v and any(x in k for x in ('KEY','TOKEN','SECRET')):text=text.replace(v,'[redacted]')
    text=re.sub(r'sk-[A-Za-z0-9_-]+','[redacted]',text)
    return re.sub(r'https?://\S+','[url]',text)[:600]

def emit(event,**data):print(json.dumps({'event':event,**data},ensure_ascii=True),flush=True)

def save(path,data):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)

def headers(key):return {'Authorization':'Bearer '+key,'Accept':'application/json'}

def error_message(response):
    try:
        data=response.json();e=data.get('error') or data.get('message') or data.get('msg') or data.get('base_resp') or data
        return safe(e.get('message',e.get('status_msg',e)) if isinstance(e,dict) else e)
    except ValueError:return 'Non-JSON error response'

async def download(client,url,destination,auth=None,max_bytes=100*1024*1024):
    if urlsplit(url).scheme!='https':raise ValueError('Media URL is not HTTPS')
    # Authorization is only supplied explicitly for the organizer's content endpoint, never media CDNs.
    response=await client.get(url,headers=auth or {},timeout=90,follow_redirects=True)
    response.raise_for_status()
    if len(response.content)>max_bytes:raise ValueError('Media exceeds benchmark size limit')
    destination.write_bytes(response.content)
    return len(response.content)

def record_start(folder,name,kind,model,channel,spec):
    path=folder/(name+'.json')
    if path.exists():
        emit('skipped_existing_receipt',name=name);return None,None,None
    row={'name':name,'kind':kind,'model':model,'channel':channel,'spec':spec,'started_at':datetime.now(timezone.utc).isoformat(),'status':'submission_pending'}
    row['usage_id']=begin(kind,model,'benchmark:'+folder.name+':'+name)
    save(path,row);emit('started',name=name,model=model)
    return path,row,time.perf_counter()

async def llm(folder,model,round_no=1,stream=False):
    name='llm-'+re.sub(r'[^a-zA-Z0-9-]','-',model)+'-'+str(round_no)+('-stream' if stream else '')
    path,row,start=record_start(folder,name,'llm',model,'organizer',{'max_tokens':768,'stream':stream,'task':'3 art-style options'})
    if not row:return
    usage={}
    try:
        body={'model':model,'messages':[{'role':'system','content':'你是漫剧策划助手。只输出符合要求的 JSON。'},{'role':'user','content':TASK}],'response_format':{'type':'json_object'},'max_tokens':768,'stream':stream}
        if model.startswith('deepseek'):body['thinking']={'type':'disabled'}
        if stream:body['stream_options']={'include_usage':True}
        async with httpx.AsyncClient(timeout=httpx.Timeout(120,connect=15),follow_redirects=False) as client:
            content='';reason=None
            if stream:
                async with client.stream('POST',BASE+'/chat/completions',headers=headers(CFG['LLM_API_KEY']),json=body) as response:
                    row['http_status']=response.status_code
                    if response.status_code>=400:
                        await response.aread();row['error']=error_message(response);row['status']='rejected';return
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):continue
                        data=line[5:].strip()
                        if data=='[DONE]':break
                        chunk=json.loads(data);usage=chunk.get('usage') or usage
                        if chunk.get('choices'):
                            choice=chunk['choices'][0];fragment=choice.get('delta',{}).get('content') or ''
                            if fragment and 'first_text_seconds' not in row:row['first_text_seconds']=round(time.perf_counter()-start,3)
                            content+=fragment;reason=choice.get('finish_reason') or reason
            else:
                response=await client.post(BASE+'/chat/completions',headers=headers(CFG['LLM_API_KEY']),json=body)
                row['http_status']=response.status_code
                if response.status_code>=400:
                    row['error']=error_message(response);row['status']='rejected';return
                data=response.json();usage=data.get('usage') or {}
                content=data['choices'][0]['message'].get('content') or '';reason=data['choices'][0].get('finish_reason')
            row['finish_reason']=reason;row['response_seconds']=round(time.perf_counter()-start,3)
            row['usage']=usage;row['output_chars']=len(content)
            (folder/(name+'.txt')).write_text(content,encoding='utf-8')
            try:
                parsed=json.loads(content);opts=parsed.get('options',[])
                row['valid_json_schema']=len(opts)==3 and all(isinstance(x,dict) and all(isinstance(x.get(k),str) and x[k].strip() for k in ('art','tone','reason')) for x in opts)
            except (ValueError,AttributeError):row['valid_json_schema']=False
            row['status']='completed' if reason=='stop' and row['valid_json_schema'] else 'invalid_output'
    except httpx.HTTPError as exc:row.update(status='uncertain',error=type(exc).__name__)
    except Exception as exc:row.update(status='invalid_output',error=safe(exc))
    finally:
        row['total_seconds']=round(time.perf_counter()-start,3)
        finish(row['usage_id'],usage,status='completed' if usage else 'rejected' if row['status']=='rejected' else 'pending')
        save(path,row);emit('result',**row)

async def image(folder,channel,model,quality='low'):
    name='image-'+channel+'-'+re.sub(r'[^a-zA-Z0-9-]','-',model)
    path,row,start=record_start(folder,name,'image',model,channel,{'size':'1024x1024','n':1,'quality':quality if channel=='organizer' else 'provider default'})
    if not row:return
    usage={}
    try:
        if channel=='organizer':
            url=BASE+'/images/generations';key=CFG['LLM_API_KEY'];body={'model':model,'prompt':PROMPT,'size':'1024x1024','n':1}
            if model.startswith('gpt-image'):body['quality']=quality
        else:
            url=CFG['IMAGE_ENDPOINT'];key=CFG['IMAGE_API_KEY'];body={'model':model,'prompt':PROMPT,'image_size':'1024x1024','batch_size':1}
        async with httpx.AsyncClient(timeout=httpx.Timeout(240,connect=15),follow_redirects=False) as client:
            response=await client.post(url,headers=headers(key),json=body);row['http_status']=response.status_code;row['response_seconds']=round(time.perf_counter()-start,3)
            if response.status_code>=400:row.update(status='rejected',error=error_message(response));return
            data=response.json();entry=(data.get('data') or data.get('images') or [])[0]
            row['response_fields']=list(entry)
            dest=folder/(name+'.png')
            if entry.get('b64_json'):dest.write_bytes(base64.b64decode(entry['b64_json']))
            elif entry.get('url'):await download(client,entry['url'],dest)
            else:raise ValueError('No image data returned')
            with Image.open(dest) as im:
                im.verify()
            with Image.open(dest) as im:row['dimensions']=list(im.size)
            row.update(status='completed',media=str(dest.relative_to(ROOT)),bytes=dest.stat().st_size)
            usage={**(data.get('usage') or {}),'images':1};row['usage']=usage
    except httpx.HTTPError as exc:row.update(status='uncertain',error=type(exc).__name__)
    except Exception as exc:row.update(status='invalid_output',error=safe(exc))
    finally:
        row['total_seconds']=round(time.perf_counter()-start,3)
        finish(row['usage_id'],usage,status='completed' if usage else 'rejected' if row['status']=='rejected' else 'pending')
        save(path,row);emit('result',**row)

async def video(folder,channel,model=None,duration=None):
    model=model or ('sora-2' if channel=='organizer' else CFG['VIDEO_MODEL'])
    name='video-'+channel+('-'+re.sub(r'[^a-zA-Z0-9-]','-',model) if model!='sora-2' and channel=='organizer' else '')
    duration=duration or (4 if channel=='organizer' else 5)
    path,row,start=record_start(folder,name,'video',model,channel,{'duration':duration,'size':'1280x720' if channel=='organizer' else '768P','reference':'text only','poll_seconds':5})
    if not row:return
    usage={}
    try:
        key=CFG['LLM_API_KEY'] if channel=='organizer' else CFG['VIDEO_API_KEY'];auth=headers(key)
        async with httpx.AsyncClient(timeout=httpx.Timeout(90,connect=15),follow_redirects=False) as client:
            if channel=='organizer':
                response=await client.post(BASE+'/videos',headers=auth,files={k:(None,str(v)) for k,v in {'model':model,'prompt':VIDEO_PROMPT,'seconds':duration,'size':'1280x720'}.items()})
            else:
                response=await client.post(CFG['VIDEO_ENDPOINT'],headers=auth,json={'model':model,'content':[{'type':'text','text':VIDEO_PROMPT}],'duration':duration,'resolution':'768P','ratio':'16:9'})
            row['http_status']=response.status_code;row['submit_seconds']=round(time.perf_counter()-start,3)
            if response.status_code>=400:row.update(status='rejected',error=error_message(response));return
            data=response.json();pid=data.get('id') if channel=='organizer' else data.get('task_id')
            if not pid:row.update(status='uncertain',error=safe(data.get('base_resp') or data.get('error') or 'No task ID returned'));return
            row.update(provider_id=pid,status='submitted');save(path,row);emit('submitted',name=name,provider_id=pid,submit_seconds=row['submit_seconds'])
            host=urlsplit(CFG['VIDEO_ENDPOINT'])
            poll_url=BASE+'/videos/'+str(pid) if channel=='organizer' else host.scheme+'://'+host.netloc+'/v2/query/video_generation/'+str(pid)
            for _ in range(120):
                await asyncio.sleep(5)
                response=await client.get(poll_url,headers=auth,timeout=30)
                if response.status_code>=400:
                    row['last_poll_error']='HTTP '+str(response.status_code);save(path,row);continue
                data=response.json();task=data if channel=='organizer' else data.get('task',{})
                status=task.get('status','unknown')
                row.update(provider_status=status,observed_seconds=round(time.perf_counter()-start,3),progress=task.get('progress'));save(path,row)
                if status in ('succeeded','completed','SUCCESS'):
                    row['generation_seconds']=round(time.perf_counter()-start,3)
                    dest=folder/(name+'.mp4')
                    if channel=='organizer':
                        media=data.get('url') or data.get('video_url')
                        if media:await download(client,media,dest)
                        else:await download(client,BASE+'/videos/'+str(pid)+'/content',dest,auth)
                    else:await download(client,task.get('content',{}).get('url'),dest)
                    # Decode a frame and read actual duration; a task ID is not a usable video.
                    import imageio_ffmpeg
                    reader=imageio_ffmpeg.read_frames(str(dest))
                    meta=next(reader);next(reader);reader.close()
                    row.update(status='completed',media=str(dest.relative_to(ROOT)),actual_duration=meta['duration'],dimensions=list(meta['size']),bytes=dest.stat().st_size)
                    usage={'duration':meta['duration']};break
                if status in ('failed','failure','FAILURE','cancelled'):
                    row.update(status='failed',error=safe(task.get('error') or task.get('message') or 'Provider generation failed'));break
            else:row.update(status='still_processing',error='Polling observation window ended; provider task ID saved, never resubmitted.')
    except httpx.HTTPError as exc:row.update(status='uncertain',error=type(exc).__name__)
    except Exception as exc:row.update(status='invalid_output',error=safe(exc))
    finally:
        row['total_seconds']=round(time.perf_counter()-start,3)
        finish(row['usage_id'],usage,status='completed' if usage else 'rejected' if row['status']=='rejected' else 'pending')
        save(path,row);emit('result',**row)

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--suite',choices=['llm','image','video'],required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--models',nargs='*',default=['gpt-5.6-sol','gpt-5.6-luna','gpt-5.4-mini','deepseek-v4-flash'])
    parser.add_argument('--rounds',type=int,default=1);parser.add_argument('--stream',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+',args.run_id):raise SystemExit('Invalid run ID')
    if not 1<=args.rounds<=3:raise SystemExit('At most three rounds')
    folder=ROOT/'data'/'benchmarks'/args.run_id;folder.mkdir(parents=True,exist_ok=True)
    init_db()
    if args.suite=='llm':
        for n in range(1,args.rounds+1):
            for model in args.models:await llm(folder,model,n,args.stream)
    elif args.suite=='image':
        await image(folder,'siliconflow',CFG['IMAGE_MODEL'])
        await image(folder,'organizer','gpt-image-1')
    else:
        await asyncio.gather(video(folder,'minimax'),video(folder,'organizer'))
if __name__=='__main__':asyncio.run(main())
