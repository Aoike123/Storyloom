"""Targeted direct-reference node checks without paid providers."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from backend import creative, worker
from backend.db import Session,Record,Task
from backend.production_nodes import queue_node,retry_node,redo_node,run_node,NODES
from backend.asset_workflow import validate_shot_identities
from test_creative import creative as _creative_fixture  # noqa: F401  (registers the fixture)


def test_storyboard_finishes_before_any_image_or_video_is_started(monkeypatch):
    """分镜节点在全部情节完成后才收尾并交给漫剧节点，期间不生成任何图片或视频。"""
    with Session.begin() as db:
        work=Record(id='work',kind='author_project',data={'director_id':'director','stage':'storyboarding','run_id':'a'})
        db.add(work)
        db.add(Record(id='director',kind='director',data={
            'board':{'shots':[{'id':'G01-S01'},{'id':'G01-S02'}]},
            'segments':{'segments':[{'id':'G01'}]},
            'units':{'G01':{'segment_id':'G01'}},
        }))
        db.add(Record(id='creative_director',kind='creative_run',data={'stage':'storyboarding','items':[]}))
        db.flush();task=queue_node(db,work,'storyboarding');task.status='running';task.owner='owner';task_id=task.id;payload=dict(task.payload)
    monkeypatch.setattr(creative,'start_reference_videos',lambda pid,segment_id=None:pytest.fail('The storyboard node must not render'))
    # 收尾本身（锁定参考图、写 visual 配置）由其他测试覆盖；这里只验证节点在收尾前不发起任何生成。
    def finish(pid):
        with Session.begin() as db:
            run=db.get(Record,'creative_'+pid);run.data={**run.data,'stage':'storyboard_ready'}
    monkeypatch.setattr(creative,'finish_storyboard',finish)
    assert run_node(task_id,payload,'owner')
    with Session() as db:
        assert db.get(Task,task_id).result['output_ids']==['G01-S01','G01-S02']
        assert db.get(Record,'work').data['stage']=='rendering'
        assert not list(db.scalars(select(Task).where(Task.kind.in_(['image','video']))))
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


def review_rejected_project(pid,issues):
    """A director project whose text pre-review rejected the board."""
    return Record(id=pid,kind='director',data={
        'review':{'approved':False,'issues':issues,'continuity':'S02 与 S03 之间缺少反应镜头',
                  'dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'}})


def test_a_rejected_pre_review_is_recorded_for_the_author_and_never_fed_back():
    """The reported failure: the reviewer's creative notes came back as "必须逐条修正" instructions.

    The text pre-review has no ground truth, so its findings are opinions. Feeding them to the
    storyboard model made it argue with the reviewer and rewrite the whole film on every attempt.
    They are recorded for the author instead, and a retry hands back only structural errors.
    """
    from test_director import board
    raw=board()
    issues=['S03 缺少对 S01 已建立规则的回应','S02 的服装与前镜不一致']
    with Session.begin() as db:
        work=Record(id='review-work',kind='author_project',data={
            'director_id':'review-director','stage':'storyboarding','run_id':'round'})
        db.add_all([work,Record(id='creative_review-director',kind='creative_run',data={'stage':'storyboarding'}),
                    review_rejected_project('review-director',issues),
                    Record(id='review-project',kind='director',data={'review':{'approved':False,'issues':issues}})])
        db.flush();previous=queue_node(db,work,'storyboarding');previous.status='needs_review'
        previous.message='分镜生成暂停：S02 的镜头未按片段时间轴输出。（错误编号 E-386558）'
    assert worker.auto_retry_node(previous.id) is True
    with Session() as db:
        work=db.get(Record,'review-work');run=db.get(Record,'creative_review-director')
        replacement=db.get(Task,work.data['supervisor'])
        assert replacement.id!=previous.id and replacement.status=='queued'
        assert run.data['stage']=='references_ready'
        # The node's own structural error is handed back; the reviewer's opinion is not.
        feedback=run.data['storyboard_retry_feedback']
        assert 'S02 的镜头未按片段时间轴输出' in feedback
        assert 'S03 缺少对 S01 已建立规则的回应' not in feedback
        assert '逐条修正' not in feedback
    # The reviewer's findings are still recorded, beside the board, for the author to judge.
    with Session() as db:
        notes=creative.review_notes(db.get(Record,'review-director'))
    assert notes['issues']==issues and notes['continuity']=='S02 与 S03 之间缺少反应镜头'


def test_repeated_failure_message_keeps_only_the_newest_error_code():
    assert worker.with_failure_code('分镜生成暂停：预审未通过。（错误编号 E-386558）','E-C0C3EF')==\
        '分镜生成暂停：预审未通过。（错误编号 E-C0C3EF）'
    assert worker.with_failure_code('第一次失败','E-AAAAAA')=='第一次失败（错误编号 E-AAAAAA）'


def test_budget_and_provider_blocks_still_skip_automatic_recovery():
    for message in ('今日共享额度不足以开始这次调用，请改用自己的 Key。',
                    '该共享供应商今日已暂停，请改用自己的 Key。',
                    '共享体验池暂未开启',
                    '尚未配置该模型的完整 API 信息。'):
        assert worker.is_retryable_failure(message) is False
    for message in ('分镜生成暂停：分镜专业预审发现问题。','分镜未按原文约定输出'):
        assert worker.is_retryable_failure(message) is True


def test_a_rejected_pre_review_is_recorded_and_the_unit_continues(_creative_fixture,monkeypatch):
    """A strict text review must not strand an episode that is otherwise ready to render.

    The review used to raise twice before giving way, which spent two full storyboard regenerations
    on opinions. Now it records its findings and the episode's board stands.
    """
    from backend import creative as c
    from test_director import board as sample_board
    issues=['G01-S02 与 G01-S03 之间缺少反应镜头']
    with Session.begin() as db:
        project=db.get(Record,'pid')
        project.data={**project.data,'board':sample_board(),
            'units':{'G01':{'segment_id':'G01','board':sample_board(),
                'review':{'approved':False,'issues':issues,'continuity':'缺少反应镜头',
                          'dramatic_logic':'通过','editability':'通过','production_feasibility':'通过'}}},
            'status':'pending_review','version':1}
        db.add(Task(id='watch-child',kind='director',status='completed',payload={'creative_id':'pid'},
                    result={'project_id':'pid'}))
        db.add(Record(id='creative_pid',kind='creative_run',data={
            'stage':'storyboarding','watch':'watch-task','items':[]}))
        db.add(Task(id='watch-task',kind='creative_watch',status='running',owner='w',
                    payload={'mode':'live','creative_id':'pid','child':'watch-child'}))
    assert c.run_watch('watch-task',{'creative_id':'pid','child':'watch-child'}) is True
    with Session() as db:
        run=db.get(Record,'creative_pid')
        notes=run.data['storyboard_review_notes']
        assert notes['issues']==['G01：'+issues[0]] and notes['approved'] is False
        # The episode itself is untouched: only the reviewer's opinion was recorded.
        assert db.get(Record,'pid').data['units']['G01']['review']['issues']==issues
        audit=[row for row in db.query(Record).filter(Record.kind=='audit').all()
               if row.data.get('action')=='storyboard_review_recorded']
        assert audit and audit[0].data['issues']==['G01：'+issues[0]]


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
        replacement=retry_node(db,work)
        assert replacement.id!=previous.id and replacement.status=='queued'
        assert replacement.payload['revision_of']==previous.id
        assert work.data['supervisor']==replacement.id
        assert child.status==watch.status=='superseded'
        assert run.data['stage']=='references_ready'
        assert 'S03 的人物与服装映射错误' in run.data['storyboard_retry_feedback']
        assert 'S03 原文顺序倒退' in run.data['storyboard_retry_feedback']
