import copy
import pytest
from backend import creative as c
from backend.db import Session,Record,Task,task_dict
from test_creative import creative,drain
from test_author_step_navigation import completed_work


def test_consecutive_edits_replace_current_card_and_reference_latest_image(creative,monkeypatch):
    completed_work(creative)
    initial=creative.get('/api/author/projects/back-work').json()
    first=initial['creative']['items'][0]
    def edit(system,payload,*args):
        if payload['schema']['title']=='AssetPromptBatch':
            return {'items':[{'asset_index':0,'prompt':payload['assets'][0]['render_contract']}]},{}
        spec=copy.deepcopy(payload['existing_spec'])
        spec['appearance']['hair_shape']=payload['feedback']
        return spec,{}
    monkeypatch.setattr(c,'chat_json',edit)
    current=first
    for feedback in ('耳上直短发，偏左分缝','耳上直短发，偏右分缝'):
        old=current
        response=creative.post('/api/author/projects/back-work/feedback',json={'task_id':old['task']['id'],'text':feedback,'confirm_paid':True})
        assert response.status_code==200;drain()
        result=creative.get('/api/author/projects/back-work').json()
        current=result['creative']['items'][0]
        assert current['task']['id']!=old['task']['id'] and current['asset']['media']!=old['asset']['media']
        assert result['creative']['version']>initial['creative']['version']
        assert result['workspace_at']<=result['progress_at']
        assert current['task']['id'] in {item['id'] for item in result['outputs']}
        assert old['task']['id'] not in {item['id'] for item in result['outputs']}
        with Session() as db:
            task=db.get(Task,current['task']['id'])
            assert task.payload['input_mode']=='text_to_image'
            assert task.payload['node_skill']['node']=='character_prompts'
            assert task.payload['revision_of']==old['task']['id']
            assert 'reference_media' not in task.payload and 'style_reference' not in task.payload
            assert db.get(Record,old['asset']['id']) is not None
    assert creative.post('/api/author/projects/back-work/feedback',json={'task_id':first['task']['id'],'text':'修改旧版本','confirm_paid':True}).status_code==409


@pytest.mark.parametrize('kind,label',[('character_sheet','人物身份参考图'),('costume_sheet','服装参考图'),('scene_sheet','场景参考图'),('dressed_character','定装参考图')])
def test_existing_image_completion_messages_describe_reference_purpose(kind,label):
    with Session.begin() as db:
        task=Task(id='old-completion',kind='image',status='completed',payload={'asset_kind':kind},message='关键帧已保存，请审核后用作视频首帧')
        db.add(task);db.flush()
        message=task_dict(task)['message']
        assert label in message and '首帧' not in message and '尾帧' not in message
