"""Retry and redo are two different actions on one stopped stage.

Retry re-runs what failed and keeps every saved record. Redo deletes this node's work and the work
of the nodes after it, then starts the node over. Continuing (``/resume``) does neither: it only
carries on, and says so when the stage still has stopped items.
"""

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


def test_retrying_a_stage_re_queues_every_stopped_clip(creative):
    """A retry re-runs every stopped clip of the stage, not only the first one."""
    rendering_stage()
    response=creative.post('/api/author/projects/back-work/retry-node',json={'confirm_paid':True})
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
    assert creative.post('/api/author/projects/back-work/retry-node',json={'confirm_paid':True}).status_code==200
    with Session() as db:
        verified=db.get(Task,'video-2')
        assert verified.status=='queued' and verified.result['provider_id']=='provider-2'
        assert '不重复提交' in verified.message
        replaced={task.payload.get('revision_of') for task in db.scalars(select(Task).where(Task.kind=='video'))}
    assert replaced-{None}=={'video-1','video-3'}


def test_continuing_a_stage_without_stopped_items_reuses_its_saved_results(creative):
    node_id=rendering_stage(clips=1,node_status='needs_review')
    with Session.begin() as db:db.get(Task,'video-1').status='completed'
    assert creative.post('/api/author/projects/back-work/resume',json={}).status_code==200
    with Session() as db:
        # Every clip is back, so continuing only needs a node to finish the stage; no clip is re-run.
        replacement=db.get(Task,db.get(Record,'back-work').data['supervisor'])
        assert replacement.id!=node_id and replacement.status=='queued'
        assert db.get(Task,node_id).status=='superseded'
        assert not list(db.scalars(select(Task).where(Task.kind=='video',Task.status=='queued')))


def test_continuing_refuses_while_a_stage_still_has_stopped_items(creative):
    """The coupling that caused the bug: 继续 used to re-run the node and rebuild its media."""
    node_id=rendering_stage()
    response=creative.post('/api/author/projects/back-work/resume',json={})
    assert response.status_code==409
    assert '重试本节点' in response.json()['detail'] and '重做本节点' in response.json()['detail']
    with Session() as db:
        # Nothing moved: the stopped clips are untouched and no replacement node was queued.
        assert db.get(Record,'back-work').data['supervisor']==node_id
        assert {db.get(Task,f'video-{index}').status for index in (1,2,3)}=={'needs_review'}
        assert not list(db.scalars(select(Task).where(Task.kind=='author_render',Task.status=='queued')))


def test_redoing_a_stage_deletes_its_work_instead_of_archiving_it(creative):
    """Redo throws the node's records and media away, so the node starts from a clean slate."""
    node_id=rendering_stage()
    with Session.begin() as db:
        for index in (1,2,3):
            db.add(Record(id=f'clip-{index}',kind='clip',data={'media':f'/media/clip-{index}.mp4','duration':5}))
            task=db.get(Task,f'video-{index}');task.result={**task.result,'clip_id':f'clip-{index}'}
        db.get(Record,'director').data={**db.get(Record,'director').data,'review':{'approved':False,'issues':['旧的复核意见']}}
    response=creative.post('/api/author/projects/back-work/redo-node',json={'confirm_paid':True})
    assert response.status_code==200,response.text
    with Session() as db:
        # The discarded tasks and the clips they produced are gone, not merely superseded.
        for index in (1,2,3):
            assert db.get(Task,f'video-{index}') is None
            assert db.get(Record,f'clip-{index}') is None
        assert db.get(Task,node_id) is None
        work=db.get(Record,'back-work');replacement=db.get(Task,work.data['supervisor'])
        assert replacement.id!=node_id and replacement.status=='queued'
        # The stage is the node being redone; its saved results were reset to the node's entry state.
        assert work.data['stage']=='rendering'
        assert work.data['production_nodes']=={'rendering':replacement.id}
        assert '本轮之前的记录已删除' in replacement.message
        assert db.get(Record,'creative_director').data['stage']=='storyboard_ready'
        audit=[row for row in db.scalars(select(Record).where(Record.kind=='audit'))
               if row.data.get('action')=='current_production_node_redone']
        assert audit and audit[0].data['phase']=='rendering'


def test_a_worker_side_rerun_keeps_the_payer_the_browser_attached(creative):
    """The reported bug: 已配置 Key 却反复提示使用算力豆.

    The worker's automatic recovery ran outside the request scope, so the replacement node was
    created with an empty payer and refused every call. The recorded database showed exactly that:
    the failed node carried ``session_id=`` while its siblings carried the browser's key session.
    """
    from backend import worker
    from backend.model_access import access_scope

    node_id=rendering_stage()
    with Session.begin() as db:
        node=db.get(Task,node_id)
        node.session_id='model_access_visitor_key_session'
        node.message='漫剧生成暂停：第 1 段失败'
        node.payload={**node.payload,'work_id':'back-work'}
    # Recovery runs with no browser in scope, exactly as the worker does it.
    with access_scope(''):
        assert worker.auto_retry_node(node_id) is True
    with Session() as db:
        replacement=db.get(Task,db.get(Record,'back-work').data['supervisor'])
        assert replacement.id!=node_id
        assert replacement.session_id=='model_access_visitor_key_session'
        rerun=[task for task in db.scalars(select(Task).where(Task.kind=='video',Task.status=='queued'))]
        assert len(rerun)==3
        assert {task.session_id for task in rerun}=={'model_access_visitor_key_session'}


def test_a_retry_only_rewrites_the_segment_that_failed(creative):
    """A retry keeps every segment that already passed, instead of paying for the whole film."""
    from test_director import board
    with Session.begin() as db:
        work=Record(id='back-work',kind='author_project',data={'title':'分镜','source_id':'source',
            'stage':'storyboarding','director_id':'director','run_id':'round'})
        db.add(work)
        db.add(Record(id='creative_director',kind='creative_run',data={'stage':'storyboarding','items':[]}))
        db.add(Record(id='director',kind='director',data={
            'board_chunk_diagnostics':{'segment_id':'G03','errors':[
                {'field':'shots.id','reason':'本片段的镜号必须接着上一片段连续编号：应为 S09、S10。'}]},
            'review':{'approved':False,'issues':['S11 的情感转折突然']}}))
        db.flush();node=queue_node(db,work,'storyboarding')
        node.status='needs_review';node.message='分镜生成暂停：G03 的镜头未按片段时间轴输出'
    assert creative.post('/api/author/projects/back-work/retry-node',json={'confirm_paid':True}).status_code==200
    with Session() as db:
        run=db.get(Record,'creative_director')
        feedback=run.data['storyboard_retry_feedback']
        assert '本片段的镜号必须接着上一片段连续编号' in feedback
        assert 'S11 的情感转折突然' not in feedback
        assert run.data['storyboard_reuse_chunks'] is True


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
