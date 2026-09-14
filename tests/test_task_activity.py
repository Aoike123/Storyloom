"""Multi-stage progress stays observable without extra model submissions."""
import asyncio
import json
from contextlib import contextmanager
import httpx
import pytest
from sqlalchemy import select
from backend import authors, providers, worker
from backend.db import Record, Session, Task, task_dict
from backend.task_activity import activity_id, preview


@pytest.fixture
def model_stream(monkeypatch):
    for key,value in {'LLM_BASE_URL':'https://models.example.test/v1','LLM_MODEL':'model',
        'LLM_API_KEY':'test-key','ALLOW_PAID_CALLS':'true'}.items():monkeypatch.setenv(key,value)
    calls=[]
    def install(values,observe=None,broken=False):
        class Chunks(httpx.SyncByteStream):
            def __iter__(self):
                def event(data):return ('data: '+json.dumps(data,ensure_ascii=False)+'\n\n').encode()
                def delta(text):return event({'choices':[{'index':0,'delta':{'content':text}}]})
                yield delta('{"display_summary":"本轮先明确创作方向，再逐项展开方案",')
                if observe:observe()
                if broken:raise httpx.RemoteProtocolError('connection dropped')
                yield delta(json.dumps(values,ensure_ascii=False)[1:])
                yield event({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})
                yield event({'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}})
                yield b'data: [DONE]\n\n'
        @contextmanager
        def stream(method,url,**kwargs):
            assert method=='POST'
            calls.append(kwargs['json'])
            yield httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks())
        monkeypatch.setattr(providers.httpx,'stream',stream)
    return install,calls


@pytest.mark.parametrize('kind,schema,values',[
    ('director','Treatment',{'premise':'日常中的异常','visual_strategy':'以光影体现差异'}),
    ('director','Board',{'shots':[{'id':'S01','scene':'房间','dramatic_action':'人物进入画面','camera':'固定镜头'}]}),
    ('director','Review',{'approved':False,'continuity':'检查相邻镜头的动作','issues':['需要核对道具位置']}),
    ('art_design','ArtPlan',{'visual_language':'柔和线条与低饱和配色','items':[{'name':'主角','facts':'来自原文','design':'保持衣着和发型一致'}]}),
    ('creative_revision','Revision',{'prompt':'保留身份与场景，仅调整衣服颜色'}),
    ('plan','Plan',{'beats':[{'title':'新的选择','narration':'人物停下脚步','reason':'回应用户修改'}]}),
])
def test_all_text_steps_stream_display_summary_and_keep_original_contract(model_stream,kind,schema,values):
    install,calls=model_stream
    with Session.begin() as db:db.add(Task(id='activity-test',kind=kind,status='running',owner='owner',payload={'mode':'live'},result={'preserved':'result-state'}))
    def observe():
        with Session() as db:
            data=task_dict(db.get(Task,'activity-test'))
            assert data['status']=='running'
            assert data['activity']['summary']=='本轮先明确创作方向，再逐项展开方案'
            assert data['result']=={'preserved':'result-state'}
    install(values,observe)
    payload={'schema':{'title':schema,'type':'object','additionalProperties':False,'properties':{},'required':[]}}
    result,usage=providers.chat_json('原有创作说明',payload,'activity-test')
    assert result==values
    assert usage['total_tokens']==30
    assert 'display_summary' not in payload['schema']['properties']
    assert 'display_summary' in json.loads(calls[0]['messages'][1]['content'])['schema']['properties']
    with Session() as db:
        activity=task_dict(db.get(Task,'activity-test'))['activity']
        assert activity['phase']=='validating'
        assert activity['items']
    assert len(calls)==1


def test_multiple_model_calls_keep_previous_summaries_after_result_replacement(model_stream):
    install,calls=model_stream
    with Session.begin() as db:db.add(Task(id='multi',kind='director',status='running',owner='owner',payload={'mode':'live'}))
    install({'premise':'第一阶段设定'})
    providers.chat_json('JSON',{'schema':{'title':'Treatment'}},'multi')
    with Session.begin() as db:db.get(Task,'multi').result={'project_id':'project'}
    install({'shots':[{'id':'S01','dramatic_action':'第二阶段动作'}]})
    providers.chat_json('JSON',{'schema':{'title':'Board'}},'multi')
    with Session() as db:
        data=task_dict(db.get(Task,'multi'))
        assert data['result']=={'project_id':'project'}
        assert data['activity']['title']=='设计分镜与镜头衔接'
        assert data['activity']['history'][0]['title']=='梳理故事与改编方向'
        assert data['activity']['history'][0]['items'][0]['text']=='第一阶段设定'
        assert len(list(db.scalars(select(Record).where(Record.kind=='usage'))))==2
    assert len(calls)==2


def test_interrupted_text_generation_keeps_summary_without_publishing(model_stream):
    install,calls=model_stream
    with Session.begin() as db:db.add(Task(id='broken',kind='art_design',status='running',owner='owner',payload={'mode':'live'}))
    install({},broken=True)
    with pytest.raises(providers.ProviderError,match='不确定'):providers.chat_json('JSON',{'schema':{'title':'ArtPlan'}},'broken')
    with Session() as db:
        assert task_dict(db.get(Task,'broken'))['activity']['summary']
        assert db.get(Task,'broken').result=={}
    assert len(calls)==1


def test_image_generation_exposes_real_stages_and_keeps_record_after_completion(monkeypatch):
    with Session.begin() as db:db.add(Task(id='image-progress',kind='image',payload={'mode':'live','title':'主角定装','prompt':'人物设定'}))
    observed=[]
    def check_phase():
        with Session() as db:observed.append(task_dict(db.get(Task,'image-progress'))['activity']['phase'])
    def generate(*args):check_phase();return 'https://example.test/image.png'
    def save(*args):check_phase();return '/media/image-progress.png'
    monkeypatch.setattr(worker,'generate_image',generate)
    monkeypatch.setattr(worker,'save_image',save)
    worker.process_one('owner')
    with Session() as db:
        task=db.get(Task,'image-progress')
        assert task.status=='completed'
        assert task.result['media']=='/media/image-progress.png'
        activity=task_dict(task)['activity']
        assert activity['type']=='media'
        assert [event['phase'] for event in activity['events']]==['generating','downloading','saving']
    assert observed==['generating','downloading']


def test_project_progress_scopes_tasks_and_shows_only_completed_current_media(client):
    with Session.begin() as db:
        db.add(Record(id='progress-work',kind='author_project',data={'stage':'preparing','director_id':'director-a','supervisor':'flow-a'}))
        db.add(Task(id='flow-a',kind='author_flow',status='waiting',payload={'work_id':'progress-work'}))
        db.add(Task(id='old-image',kind='image',status='completed',payload={'creative_id':'director-a'},result={'media':'/media/old.png'}))
        db.add(Task(id='new-image',kind='image',status='completed',payload={'creative_id':'director-a','revision_of':'old-image','title':'新画面'},result={'media':'/media/new.png'}))
        db.add(Task(id='video-waiting',kind='video',status='waiting',payload={'director_id':'director-a'},result={'media':'/media/not-ready.mp4'}))
        db.add(Task(id='foreign',kind='image',status='completed',payload={'director_id':'director-b'},result={'media':'/media/foreign.png'}))
    progress=authors.project_progress('progress-work')
    assert progress['task']['id']=='flow-a'
    assert {task['id'] for task in progress['jobs']}=={'old-image','new-image','video-waiting'}
    assert progress['outputs']==[{'id':'new-image','kind':'image','title':'新画面','media':'/media/new.png'}]
    assert 'source' not in progress
    assert client.get('/api/author/projects/missing/events').status_code==404


def test_project_stream_updates_between_stages_and_stops_on_disconnect():
    with Session.begin() as db:
        db.add(Record(id='stream-project',kind='author_project',data={'stage':'preparing','director_id':'director-a'}))
        db.add(Task(id='stream-job',kind='art_design',status='running',payload={'creative_id':'director-a'}))
    class Request:
        disconnected=False
        async def is_disconnected(self):return self.disconnected
    async def read():
        request=Request();response=await authors.project_events('stream-project',request)
        body=response.body_iterator
        first=json.loads((await anext(body)).removeprefix('data: '))
        assert first['jobs'][0]['status']=='running'
        with Session.begin() as db:
            db.get(Task,'stream-job').status='completed'
            db.get(Record,'stream-project').data={'stage':'assets_review','director_id':'director-a'}
        last=json.loads((await anext(body)).removeprefix('data: '))
        assert last['stage']=='assets_review'
        assert last['jobs'][0]['status']=='completed'
        request.disconnected=True
        await body.aclose()
    asyncio.run(read())


def test_preview_whitelists_human_readable_fields():
    draft=preview(json.dumps({'display_summary':'创作摘要','api_key':'private','raw_prompt':'private','shots':[{'id':'S01','dramatic_action':'人物入场','internal':'private'}]}))
    assert 'private' not in json.dumps(draft)
    assert draft['items'][0]['text']=='人物入场'
