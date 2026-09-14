import pytest
from fastapi import HTTPException

from backend import authors,preproduction as prep
from backend.db import Record,Session,Task


def test_direct_reference_snapshot_rejects_a_changed_base_task_pointer():
    with Session.begin() as db:
        db.add(Record(id='binding-project',kind='director',data={'status':'awaiting_preproduction'}))
        db.add(Record(id='base-old-asset',kind='asset',version=4,data={'status':'approved'}))
        db.add(Task(id='base-old',kind='image',status='completed',result={'asset_id':'base-old-asset'}))
        db.add(Record(id='creative_binding-project',kind='creative_run',data={
            'direct_reference_inputs':True,'items':[{'task_id':'base-old'}]}))
        db.add(Record(id='prep_binding-project',kind='preproduction',data={
            'style':'固定漫画画风','assets':{'base-old-asset':{'role':'scene'}},
            'versions':{'base-old-asset':4},'source_dependencies':[],
            'source_assets':{'base-old':{'asset_id':'base-old-asset','asset_version':4}}}))
    with Session() as db:assert prep.snapshot(db,'binding-project')[0].id=='prep_binding-project'
    with Session.begin() as db:
        db.add(Record(id='base-new-asset',kind='asset',data={'status':'approved'}))
        db.add(Task(id='base-new',kind='image',status='completed',result={'asset_id':'base-new-asset'}))
        run=db.get(Record,'creative_binding-project');run.data={**run.data,'items':[{'task_id':'base-new'}]}
    with Session() as db:
        with pytest.raises(HTTPException,match='基础素材已重做'):
            prep.snapshot(db,'binding-project')


def test_invalidation_archives_board_and_closes_every_current_gate():
    with Session.begin() as db:
        db.add(Record(id='invalidate-project',kind='director',version=7,data={
            'status':'approved','treatment':{'premise':'保留'},'board':{'shots':[]},
            'review':{'approved':True},'board_diagnostics':{'raw':{}},'preproduction_stamp':'old-stamp'}))
        db.add(Record(id='prep_invalidate-project',kind='preproduction',data={'versions':{},'assets':{}}))
        db.add(Record(id='prep_gate_invalidate-project',kind='preproduction_gate',data={'stamp':'old-stamp'}))
        db.add(Record(id='visual_invalidate-project',kind='visual_config',data={'approved':True,'bindings':{}}))
        db.add(Record(id='visual_gate_invalidate-project',kind='visual_gate',data={'stamp':'visual-old'}))
        prep.invalidate_downstream_references(db,'invalidate-project','单项素材换代','replacement-image')
    with Session() as db:
        project=db.get(Record,'invalidate-project')
        assert project.data['status']=='awaiting_preproduction' and 'board' not in project.data
        assert project.data['board_history'][-1]['preproduction_stamp']=='old-stamp'
        assert all(db.get(Record,rid).data.get('invalidated_at') for rid in (
            'prep_invalidate-project','prep_gate_invalidate-project','visual_invalidate-project','visual_gate_invalidate-project'))
        assert db.get(Record,'visual_invalidate-project').data['approved'] is False


def test_current_outputs_hide_old_director_versions_but_keep_base_assets():
    tasks=[
        Task(id='base',kind='image',status='completed',payload={'creative_id':'output-project'},result={'media':'/media/base.png'}),
        Task(id='old-shot',kind='image',status='completed',payload={
            'director_id':'output-project','director_version':2,'shot_id':'S01'},result={'media':'/media/old.png'}),
        Task(id='current-shot',kind='image',status='completed',payload={
            'director_id':'output-project','director_version':3,'shot_id':'S01'},result={'media':'/media/current.png'}),
    ]
    assert [item['id'] for item in authors.completed_outputs(tasks,'output-project',3)]==['base','current-shot']
