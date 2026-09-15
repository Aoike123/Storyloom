"""Targeted direct-reference node checks without paid providers."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from backend import creative, worker
from backend.db import Session,Record,Task
from backend.production_nodes import queue_node,retry_current_node,run_node,NODES
from backend.asset_workflow import validate_shot_identities


def test_storyboard_finishes_before_any_image_or_video_is_started(monkeypatch):
    with Session.begin() as db:
        work=Record(id='work',kind='author_project',data={'director_id':'director','stage':'storyboarding','run_id':'a'})
        db.add(work)
        db.add(Record(id='director',kind='director',data={'board':{'shots':[{'id':'S01'},{'id':'S02'}]}}))
        db.add(Record(id='creative_director',kind='creative_run',data={'stage':'storyboard_ready','items':[]}))
        db.flush();task=queue_node(db,work,'storyboarding');task.status='running';task.owner='owner';task_id=task.id;payload=dict(task.payload)
    monkeypatch.setattr(creative,'start_reference_videos',lambda pid:pytest.fail('The storyboard node must not render'))
    assert run_node(task_id,payload,'owner')
    with Session() as db:
        assert db.get(Task,task_id).result['output_ids']==['S01','S02']
        assert db.get(Record,'work').data['stage']=='rendering'
        assert not list(db.scalars(select(Task).where(Task.kind.in_(['image','video','author_composite']))))
    assert list(NODES)==['storyboarding','rendering']


def test_raw_identity_and_costume_references_must_be_bound_together():
    refs={
        'alice':{'role':'character','identity_asset_id':'alice','requires_costume':True},
        'bob':{'role':'character','identity_asset_id':'bob','requires_costume':True},
        'alice-shirt':{'role':'costume','identity_asset_id':'alice'},
        'scene':{'role':'scene'},
    }
    validate_shot_identities(['alice','alice-shirt','scene'],refs)
    with pytest.raises(HTTPException):validate_shot_identities(['alice','scene'],refs)
    with pytest.raises(HTTPException):validate_shot_identities(['bob','alice-shirt','scene'],refs)


def test_interrupted_node_is_not_automatically_resubmitted():
    with Session.begin() as db:db.add(Task(id='interrupted',kind='author_storyboard',status='running',lease=0,payload={'mode':'live'}))
    assert worker.claim('new-owner') is None
    with Session() as db:assert db.get(Task,'interrupted').status=='needs_review'


def test_leftover_shot_reference_image_is_retired_instead_of_submitted():
    """The removed storyboard image step must never spend money on a leftover queued task."""
    with Session.begin() as db:
        db.add(Task(id='stale-frame',kind='image',status='queued',payload={
            'mode':'live','director_id':'director-1','shot_id':'S01','consistency_stamp':'v1',
            'reference_media':['/media/ref.png']}))
        db.add(Task(id='usable',kind='video',status='queued',payload={'mode':'live','director_id':'director-1'}))
    assert worker.claim('cleanup-owner')=='usable'
    with Session() as db:
        stale=db.get(Task,'stale-frame')
        assert stale.status=='cancelled' and '参考图直接生视频' in stale.message


def test_failed_node_recovers_once_automatically_then_waits_for_a_person():
    from test_director import TEXT,board
    raw=board()
    for shot,source_ref in zip(raw['shots'],['P001','P004','P002','P003']):shot['source_ref']=source_ref
    with Session.begin() as db:
        work=Record(id='auto-work',kind='author_project',data={
            'director_id':'auto-director','stage':'storyboarding','run_id':'round'})
        run=Record(id='creative_auto-director',kind='creative_run',data={'stage':'storyboarding'})
        db.add_all([work,run,Record(id='auto-director',kind='director',data={'board_diagnostics':{'raw':raw}})])
        db.flush();previous=queue_node(db,work,'storyboarding').id
    with Session.begin() as db:
        task=db.get(Task,previous);task.status='failed';task.message='分镜节点失败'
    assert worker.auto_retry_node(previous) is True
    with Session() as db:
        work=db.get(Record,'auto-work');replacement=db.get(Task,work.data['supervisor'])
        assert replacement.id!=previous and replacement.payload['auto_retry_count']==1
        assert replacement.status=='queued'
    with Session.begin() as db:
        task=db.get(Task,replacement.id);task.status='failed';task.message='分镜节点再次失败'
    assert worker.auto_retry_node(replacement.id) is False


def test_node_failure_caused_by_missing_budget_does_not_auto_retry():
    with Session.begin() as db:
        work=Record(id='budget-work',kind='author_project',data={
            'director_id':'budget-director','stage':'storyboarding','run_id':'round'})
        db.add_all([work,Record(id='creative_budget-director',kind='creative_run',data={'stage':'storyboarding'})])
        db.flush();task=queue_node(db,work,'storyboarding')
        task.status='needs_review';task.message='今日共享额度不足以开始这次调用，请改用自己的 Key。'
    assert worker.auto_retry_node(task.id) is False


def test_automatic_board_repairs_are_named_instead_of_silent():
    """An inferred binding must be reported as a code correction, not passed off as model output."""
    message=worker.repair_message({'changes':[
        {'shot_id':'S01','action':'bind_named_character','asset_id':'trainer'},
        {'shot_id':'S02','action':'bind_unique_named_scene','asset_id':'room'},
        {'shot_id':'S02','action':'remove_duplicate_assets','asset_ids':['costume']},
    ]})
    assert message.startswith('模型原稿经代码校正')
    assert '按画面文字补绑人物（trainer）' in message
    assert '补绑场景（room）' in message and '去重参考图（costume）' in message
    assert worker.repair_message(None)=='分镜参考已按确定映射校正'


def test_append_only_histories_stay_bounded():
    from backend.db import EVENT_LIMIT, HISTORY_LIMIT, bounded

    events=[]
    for index in range(EVENT_LIMIT+25):
        events=bounded(events,{'message':index},EVENT_LIMIT)
    assert len(events)==EVENT_LIMIT
    assert events[-1]['message']==EVENT_LIMIT+24 and events[0]['message']==25
    assert len(bounded([], 'only', HISTORY_LIMIT))==1


def test_failed_storyboard_child_exposes_a_fresh_current_node_retry_with_feedback():
    from test_director import TEXT,board
    raw=board()
    for shot,source_ref in zip(raw['shots'],['P001','P004','P002','P003']):shot['source_ref']=source_ref
    with Session.begin() as db:
        work=Record(id='retry-work',kind='author_project',data={
            'director_id':'retry-director','stage':'storyboarding','run_id':'round'})
        run=Record(id='creative_retry-director',kind='creative_run',data={'stage':'storyboarding'})
        db.add_all([work,run,Record(id='retry-director',kind='director',data={
            'board_diagnostics':{'raw':raw}})]);db.flush()
        previous=queue_node(db,work,'storyboarding');previous.status='waiting';db.flush()
        child=Task(id='retry-child',kind='director',status='needs_review',message='S03 的人物与服装映射错误',payload={
            'stage':'board','project_id':'retry-director','production_phase':'storyboarding','production_node':previous.id,
            'source':{'content':TEXT}})
        watch=Task(id='retry-watch',kind='creative_watch',status='needs_review',message='自动分镜未完成',payload={
            'creative_id':'retry-director','child':child.id,'production_phase':'storyboarding','production_node':previous.id})
        db.add_all([child,watch]);run.data={**run.data,'watch':watch.id};db.flush()
        replacement=retry_current_node(db,work)
        assert replacement.id!=previous.id and replacement.status=='queued'
        assert replacement.payload['revision_of']==previous.id
        assert work.data['supervisor']==replacement.id
        assert child.status==watch.status=='superseded'
        assert run.data['stage']=='references_ready'
        assert 'S03 的人物与服装映射错误' in run.data['storyboard_retry_feedback']
        assert 'S03 原文顺序倒退' in run.data['storyboard_retry_feedback']
