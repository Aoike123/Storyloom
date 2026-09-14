import copy
import httpx
import pytest
from backend import creative as c,worker,image_provider as ip
from backend.db import Session,Task,task_dict
from backend.reference_image_model import REFERENCE_IMAGE_MODEL
from test_creative import creative,drain
from test_author_step_navigation import completed_work


def test_feedback_rewrites_spec_then_runs_a_fresh_full_prompt(creative,monkeypatch):
    completed_work(creative)
    item=next(item for item in creative.get('/api/author/projects/back-work').json()['creative']['items'] if item['role']=='costume')
    changed=copy.deepcopy(item['asset_spec']);changed['wardrobe'][0]['color']='#808080'
    calls=[]
    def compile_prompt(system,payload,*a,**k):
        calls.append(payload['schema']['title'])
        if payload['schema']['title']=='CostumeSheet':return copy.deepcopy(changed),{}
        assert payload['schema']['title']=='AssetPromptBatch'
        contract=payload['assets'][0]['render_contract']
        assert '#808080' in contract and '细颗粒绘图纸纹理' in contract
        assert payload['target']['model']=='Tongyi-MAI/Z-Image-Turbo'
        return {'items':[{'asset_index':0,'prompt':contract}]},{}
    monkeypatch.setattr(c,'chat_json',compile_prompt)
    generated=[]
    monkeypatch.setattr(worker,'generate_image',lambda prompt,tid:(generated.append((prompt,tid)) or 'https://example.test/new.png'))
    monkeypatch.setattr(worker,'generate_from_references',lambda *a,**k:pytest.fail('Base asset feedback must not use an old image'))

    previous=item
    response=creative.post('/api/author/projects/back-work/feedback',json={
        'task_id':previous['task']['id'],'text':'上衣改成灰色','confirm_paid':True})
    assert response.status_code==200;drain()
    current=next(item for item in creative.get('/api/author/projects/back-work').json()['creative']['items'] if item['role']=='costume')
    with Session() as db:
        task=db.get(Task,current['task']['id']);public=task_dict(task)['generation']
        assert task.payload['input_mode']=='text_to_image'
        assert task.payload['regeneration_mode']=='full_prompt'
        assert task.payload['revision_instruction']=='上衣改成灰色'
        assert task.payload['node_skill']['node']=='asset_prompts'
        assert '#808080' in task.payload['prompt'] and '细颗粒绘图纸纹理' in task.payload['prompt']
        for key in ('reference_media','image_model','style_reference','edit_mode','edit_instruction'):
            assert key not in task.payload
        assert public['revision_instruction']=='上衣改成灰色'
    assert generated and generated[0][0]==current['prompt']
    assert calls==['CostumeSheet','AssetPromptBatch']


def test_reference_composition_transport_is_separate_from_feedback_redraw(creative,monkeypatch):
    completed_work(creative)
    item=creative.get('/api/author/projects/back-work').json()['creative']['items'][0]
    cfg={'IMAGE_PROVIDER':'siliconflow','IMAGE_MODEL':'Tongyi-MAI/Z-Image-Turbo',
         'IMAGE_ENDPOINT':'https://example.test/images','IMAGE_API_KEY':'not-for-output'}
    monkeypatch.setattr(ip,'model_config',lambda:cfg)
    monkeypatch.setattr(ip,'reserve_call',lambda *a,**k:None)
    sent=[]
    def post(url,**kwargs):sent.append(kwargs['json']);return httpx.Response(200,json={'images':[{'url':'https://example.test/result.png'}]})
    monkeypatch.setattr(ip.httpx,'post',post)
    with Session.begin() as db:db.add(Task(id='reference-compose',kind='image',status='running'))
    ip.generate_from_references('组合已批准的人物与服装','reference-compose',references=[item['asset']['media']])
    assert sent[0]['model']==REFERENCE_IMAGE_MODEL
    assert set(sent[0])=={'model','prompt','image','num_inference_steps'}
    with Session() as db:
        saved=db.get(Task,'reference-compose').result['generation_request']
        assert saved['input_mode']=='reference_images' and saved['reference_count']==1
    with pytest.raises(ip.ProviderError):
        ip.generate_from_references('非法模型','bad-model',model='Qwen/Qwen-Image-Edit',references=[item['asset']['media']])
    assert len(sent)==1
