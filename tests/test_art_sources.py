import pytest
from sqlalchemy import select

from backend import creative as c, worker
from backend.db import Record, Session, Task
from test_creative import creative, drain
from asset_spec_fixtures import plan


@pytest.mark.parametrize('reference,expected', [
    ('P001', [1]),
    (' P001 、 P003, P005；P007 ', [1, 3, 5, 7]),
    ('P001-P003、P005-P009、P012-P013', [1, 2, 3, 5, 6, 7, 8, 9, 12, 13]),
    ('P007-P009、P012-P013', [7, 8, 9, 12, 13]),
    ('P001–P003、P002、P005', [1, 2, 3, 5]),
])
def test_art_citations_preserve_all_explicit_passages(reference, expected):
    passages = {f'P{i:03}': str(i) for i in range(1, 14)}
    assert c.source_refs(reference, passages) == [f'P{i:03}' for i in expected]


@pytest.mark.parametrize('reference', [
    '', 'P001、', 'P001-P999999999', 'P003-P001', 'P001、P099',
    'P001 or any other passage', 'P001-P003-extra', '第一段',
])
def test_art_citations_reject_unknown_or_ambiguous_references(reference):
    with pytest.raises(c.ProviderError):
        c.source_refs(reference, {'P001': '第一段', 'P002': '第二段', 'P003': '第三段'})


def test_art_citation_range_rejects_missing_middle_passage():
    with pytest.raises(c.ProviderError, match='P002'):
        c.source_refs('P001-P003', {'P001': '第一段', 'P003': '第三段', 'P004': '第四段'})


def failed_design(client):
    content = '甲' * 240 + '乙' * 240 + '丙' * 80
    with Session.begin() as db:
        original = db.get(Task, 'origin')
        original.payload = {**original.payload, 'source': {'content': content}}
    response = client.post('/api/creative/pid/design', json={'art': '手绘漫画', 'tone': '温馨', 'confirm_paid': True})
    assert response.status_code == 200
    task_id = response.json()['id']
    raw = plan()
    raw['items'][0]['source_ref']='P001-P002、P003'
    raw['items'][1]['source_ref']='P002、P003'
    with Session.begin() as db:
        task = db.get(Task, task_id)
        task.payload={k:v for k,v in task.payload.items() if k!='skill_pipeline'}  # Historical checkpoint, before the prompt-writing node.
        task.status = 'needs_review'; task.attempts = 1
        task.message = '角色或场景的原文依据无法定位，已停止。'
        run = db.get(Record, 'creative_pid')
        run.data = {**run.data, 'raw_design': raw}
        db.add(Task(id='stopped-flow', kind='author_flow', status='needs_review'))
        db.add(Record(id='work', kind='author_project', data={
            'director_id': 'pid', 'stage': 'preparing', 'supervisor': 'stopped-flow',
        }))
    return task_id, raw, content


def test_resume_uses_saved_design_without_second_model_call_or_duplicate_images(creative, monkeypatch):
    task_id, raw, content = failed_design(creative)
    monkeypatch.setattr(c, 'chat_json', lambda *a, **k: pytest.fail('A saved model result must not be resubmitted'))
    assert creative.post('/api/author/projects/work/resume').status_code == 200
    assert creative.post('/api/author/projects/work/resume').status_code == 409
    assert worker.process_one('art-recovery-test')
    with Session() as db:
        task = db.get(Task, task_id)
        assert task.status == 'completed' and task.attempts == 2
        data = db.get(Record, 'creative_pid').data
        assert data['raw_design'] == raw
        assert data['items'][0]['source_refs'] == ['P001', 'P002', 'P003']
        assert data['items'][0]['source_quote'] == '\n'.join([content[:240], content[240:480], content[480:]])
        assert data['items'][1]['source_refs'] == ['P002', 'P003']
        payload = dict(task.payload)
    c.run_design(task_id, payload)  # Replaying the committed local step is harmless.
    with Session() as db:
        assert len(list(db.scalars(select(Task).where(Task.kind == 'image')))) == 3
    drain()
    with Session.begin() as db:
        for task in db.scalars(select(Task).where(Task.kind == 'author_flow', Task.status == 'waiting')):
            task.lease = 0
    drain()
    assert creative.get('/api/author/projects/work').json()['stage'] == 'assets_review'


@pytest.mark.parametrize('problem', ['unknown_ref', 'missing_draft', 'other_task'])
def test_resume_does_not_queue_unsafe_saved_design(creative, monkeypatch, problem):
    task_id, raw, _ = failed_design(creative)
    with Session.begin() as db:
        run = db.get(Record, 'creative_pid')
        if problem == 'unknown_ref':
            raw['items'][0]['source_ref'] = 'P001、P099'
            run.data = {**run.data, 'raw_design': raw}
        elif problem == 'missing_draft':
            run.data = {k: v for k, v in run.data.items() if k != 'raw_design'}
        else:
            run.data = {**run.data, 'raw_design_task_id': 'another-task'}
    monkeypatch.setattr(c, 'chat_json', lambda *a, **k: pytest.fail('Recovery must never resubmit the model'))
    response = creative.post('/api/author/projects/work/resume')
    assert response.status_code == 409
    if problem == 'unknown_ref':
        assert '女主' in response.json()['detail'] and 'P099' in response.json()['detail']
    with Session() as db:
        assert db.get(Task, task_id).status == 'needs_review'
        assert db.get(Record, 'work').data['supervisor'] == 'stopped-flow'
        assert not list(db.scalars(select(Task).where(Task.kind == 'image')))
