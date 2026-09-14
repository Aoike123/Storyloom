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
    monkeypatch.setattr(creative,'start_reference_frames',lambda pid:pytest.fail('The storyboard node must not render'))
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
