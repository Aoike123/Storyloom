"""One stopped stage is repaired as a whole, not one item at a time."""

import pytest
from sqlalchemy import select
from backend.db import Session,Record,Task
from backend.production_nodes import failure_summary,queue_node,run_node
from test_creative import creative,drain


def rendering_stage(clips=3,node_status='needs_review',submitted=()):
    """An author work whose rendering stage stopped with several clips still unfinished."""
    with Session.begin() as db:
        work=Record(id='back-work',kind='author_project',data={'title':'其它资产','source_id':'source',
            'stage':'rendering','director_id':'director','run_id':'round'})
        db.add(work)
        db.add(Record(id='creative_director',kind='creative_run',data={'stage':'videos_review','items':[]}))
        db.add(Record(id='director',kind='director',data={'board':{'shots':[{'id':f'S0{i}'} for i in range(1,clips+1)]}}))
        db.flush();node=queue_node(db,work,'rendering')
        node.status=node_status
        node.message='漫剧生成暂停：第 1 段失败（错误编号 E-AAAAAA）'
        work.data={**work.data,'supervisor':node.id,'production_nodes':{'rendering':node.id}}
        for index in range(1,clips+1):
            db.add(Task(id=f'video-{index}',kind='video',status='needs_review',
                message=f'第 {index} 段失败（错误编号 E-{index}）',
                result={'provider_id':f'provider-{index}'} if index in submitted else {},
                payload={'mode':'live','director_id':'director','shot_id':f'S0{index}',
                         'production_phase':'rendering','production_node':node.id}))
    return node.id


def test_continuing_a_stage_re_queues_every_stopped_clip(creative):
    """The reported failure: continuing only re-ran the stage, so it stopped on the first error again."""
    rendering_stage()
    response=creative.post('/api/author/projects/back-work/resume',json={})
    assert response.status_code==200,response.text
    with Session() as db:
        replacement=db.get(Task,db.get(Record,'back-work').data['supervisor'])
        assert replacement.status=='queued' and replacement.kind=='author_render'
        queued=[task for task in db.scalars(select(Task).where(Task.kind=='video',Task.status=='queued'))]
        # Every clip gets a new version that points back at the one it replaces.
        assert {task.payload['revision_of'] for task in queued}=={'video-1','video-2','video-3'}
        assert all(task.payload['production_node']==replacement.id for task in queued)
        assert {db.get(Task,f'video-{index}').status for index in (1,2,3)}=={'superseded'}


def test_a_submitted_clip_is_verified_instead_of_paid_again(creative):
    """A clip the provider already accepted keeps its own task, so no second submission happens."""
    rendering_stage(submitted=(2,))
    assert creative.post('/api/author/projects/back-work/resume',json={}).status_code==200
    with Session() as db:
        verified=db.get(Task,'video-2')
        assert verified.status=='queued' and verified.result['provider_id']=='provider-2'
        assert '不重复提交' in verified.message
        replaced={task.payload.get('revision_of') for task in db.scalars(select(Task).where(Task.kind=='video'))}
    assert replaced-{None}=={'video-1','video-3'}


def test_a_stage_without_stopped_items_continues_on_its_saved_results(creative):
    node_id=rendering_stage(clips=1,node_status='failed')
    with Session.begin() as db:db.get(Task,'video-1').status='completed'
    assert creative.post('/api/author/projects/back-work/resume',json={}).status_code==200
    with Session() as db:
        replacement=db.get(Task,db.get(Record,'back-work').data['supervisor'])
        assert replacement.id!=node_id and replacement.status=='queued'
        assert not list(db.scalars(select(Task).where(Task.kind=='video',Task.status=='queued')))


def test_the_stage_message_names_every_stopped_item():
    """The stage used to report one error while other clips waited for a decision of their own."""
    rendering_stage()
    with Session.begin() as db:
        work=db.get(Record,'back-work');node=db.get(Task,work.data['supervisor'])
        node.status='running';node.owner='owner'
    with pytest.raises(Exception) as raised:
        run_node(node.id,{'phase':'rendering','work_id':'back-work','run_id':'round'},'owner')
    detail=str(raised.value)
    assert '漫剧生成暂停' in detail
    for index in (1,2,3):assert f'第 {index} 段失败' in detail


def test_a_long_list_of_failures_stays_readable():
    tasks=[Task(id=f'clip-{index}',kind='video',message=f'第 {index} 段失败',payload={'shot_id':f'S{index:02d}'})
           for index in range(1,10)]
    summary=failure_summary(tasks)
    assert summary.startswith('S01：第 1 段失败') and '另有 3 项未列出' in summary
    assert len(failure_summary(tasks,limit=60))<=60


def test_every_picture_of_one_round_comes_back_in_one_action(creative,monkeypatch):
    """The picture panel offers one button per picture; the whole set takes one visitor action."""
    from test_image_recovery import accepting_pictures,own_key_access,stopped_by_the_operator_key
    items,_=stopped_by_the_operator_key(creative,monkeypatch)
    headers=own_key_access(creative)
    accepting_pictures(monkeypatch)
    response=creative.post('/api/author/projects/back-work/images/retry',json={'confirm_paid':True},headers=headers)
    assert response.status_code==200,response.text
    assert len(response.json()['tasks'])==len(items)
    drain()
    work=creative.get('/api/author/projects/back-work',headers=headers).json()
    assert not work['retryable_images'] and work['stage']=='assets_review'
    assert all(item['asset'] for item in work['creative']['items'])
    # Nothing is left to repair, so a second click must not queue another round.
    again=creative.post('/api/author/projects/back-work/images/retry',json={'confirm_paid':True},headers=headers)
    assert again.status_code==409
