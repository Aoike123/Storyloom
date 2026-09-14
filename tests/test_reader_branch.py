import shutil
import time

import pytest
from sqlalchemy import select

from backend import reader_branch as branch
from backend import worker
from backend.db import DATA, Record, Session, Task


SESSION = 'reader_session_1234567890'


def _plan(*, terminal=False, rejoin_index=1):
    return {
        'accepted': True, 'conflict': '', 'summary': '选择产生直接后果并得到回应',
        'causal_chain': ['人物执行选择', '现场状态随之改变', '人物对结果作出反应'],
        'terminal': terminal, 'rejoin_index': None if terminal else rejoin_index,
        'shots': [{
            'id': 'B01', 'purpose': 'consequence', 'action': '人物在原地执行新的选择',
            'dialogue': '', 'causal_result': '选择的直接后果在当前场景中出现',
            'continuity_in': '沿用暂停时的人物位置和道具状态',
            'continuity_out': '人物看见并理解刚刚造成的结果', 'generation_seconds': 5,
            'uses_new_scene': False, 'uses_new_character': False, 'changes_costume': False,
        }],
    }


def test_branch_queues_only_locked_reference_videos(client, monkeypatch, sample_video):
    source = DATA / 'media' / 'branch-source.mp4'
    shutil.copyfile(sample_video, source)
    with Session.begin() as db:
        db.add(Record(id='release_branch', kind='reader_release', data={
            'title': '分支故事', 'description': '一次选择带来后果',
            'entries': [
                {'clip_id': 'base-1', 'media': '/media/branch-source.mp4', 'start': 0, 'end': 2.5, 'shot_id': 'S01'},
                {'clip_id': 'base-2', 'media': '/media/branch-source.mp4', 'start': 2.5, 'end': 5, 'shot_id': 'S02'},
            ],
        }))
    monkeypatch.setattr(branch, 'settings', lambda: {
        'paid_enabled': True, 'llm_paid_enabled': True, 'video_paid_enabled': True,
        'llm_configured': True, 'video_configured': True,
    })
    monkeypatch.setattr(branch, 'model_config', lambda: {'VIDEO_PROVIDER': 'minimax'})
    response = client.post('/api/reader/branches', headers={'X-Reader-Session': SESSION}, json={
        'release_id': 'release_branch', 'index': 0, 'offset': 1.25,
        'text': '先把证据藏起来，再回应对方', 'confirm_generation': True,
    })
    assert response.status_code == 200, response.text
    branch_id = response.json()['id']
    with Session.begin() as db:
        row = db.get(Record, branch_id)
        plan_task_id = row.data['plan_task_id']
        plan_task = db.get(Task, plan_task_id)
        plan_task.status = 'running'

    def fake_call(_chat, _node, _payload, _task_id, **kwargs):
        return kwargs['validator'](_plan()), {}

    monkeypatch.setattr(branch, 'call_node', fake_call)
    with Session() as db:
        payload = dict(db.get(Task, plan_task_id).payload)
    branch.run_plan(plan_task_id, payload)

    with Session() as db:
        row = db.get(Record, branch_id)
        tasks = [db.get(Task, item['task_id']) for item in row.data['video_tasks']]
        assert len(tasks) == 1 and tasks[0].kind == 'video'
        assert not list(db.scalars(select(Task).where(Task.kind == 'image')))
        assert tasks[0].payload['input_snapshot']['constraints'] == {
            'new_scene': False, 'new_character': False, 'costume_change': False,
        }
        assert tasks[0].payload['reference_media'][0].endswith('/cut.png')

    status = client.get(f'/api/reader/branches/{branch_id}', headers={'X-Reader-Session': SESSION}).json()
    assert status['status'] == 'generating'
    assert [entry['status'] for entry in status['entries']] == ['pending', 'ready']
    assert client.get(f'/api/reader/branches/{branch_id}', headers={
        'X-Reader-Session': 'another_reader_session_12345',
    }).status_code == 404


def test_story_end_plan_cannot_force_original_ending():
    with pytest.raises(ValueError, match='不能强行回到原结局'):
        branch._plan_contract(_plan(), min_rejoin_index=2, entry_count=3, at_end=True, max_shots=3)
    accepted = branch._plan_contract(_plan(terminal=True), min_rejoin_index=2, entry_count=3, at_end=True, max_shots=3)
    assert accepted.terminal and accepted.rejoin_index is None
    visual_change = _plan(terminal=True)
    visual_change['shots'][0]['action'] = '转场到另一个场景并换上新衣服'
    with pytest.raises(ValueError, match='超出锁定视觉范围'):
        branch._plan_contract(visual_change, min_rejoin_index=2, entry_count=3, at_end=True, max_shots=3)


def test_reader_branch_videos_have_dedicated_worker_lanes():
    with Session.begin() as db:
        db.add_all([
            Task(id='lane-branch-video', kind='video', created=1,
                 payload={'reader_branch_id': 'branch-lane', 'prompt': '分支视频'}),
            Task(id='lane-general-plan', kind='reader_branch_plan', created=2, payload={}),
        ])
    assert worker.claim('branch-owner', 'branch') == 'lane-branch-video'
    assert worker.claim('general-owner', 'general') == 'lane-general-plan'


def test_all_branch_segments_are_submitted_before_polling(monkeypatch):
    calls = []

    def submit(_prompt, task_id, *_args, **_kwargs):
        calls.append(task_id)
        return 'provider-' + task_id

    monkeypatch.setattr(worker, 'submit_video', submit)
    monkeypatch.setattr(worker, 'poll_video', lambda *_args: pytest.fail('提交完同一分支前不应轮询视频'))
    with Session.begin() as db:
        db.add_all([
            Task(id='batch-B01', kind='video', created=1, payload={
                'mode': 'live', 'reader_branch_id': 'branch-batch', 'branch_shot_id': 'B01',
                'generation_seconds': 5, 'prompt': '第一段', 'input_snapshot': {'asset_bindings': []},
            }),
            Task(id='batch-B02', kind='video', created=2, payload={
                'mode': 'live', 'reader_branch_id': 'branch-batch', 'branch_shot_id': 'B02',
                'generation_seconds': 5, 'prompt': '第二段', 'input_snapshot': {'asset_bindings': []},
            }),
        ])

    assert worker.process_one('batch-owner-1', 'branch')
    with Session() as db:
        assert db.get(Task, 'batch-B01').status == 'waiting'
        assert db.get(Task, 'batch-B02').status == 'queued'
    assert worker.process_one('batch-owner-2', 'branch')

    with Session() as db:
        first, second = db.get(Task, 'batch-B01'), db.get(Task, 'batch-B02')
        assert calls == ['batch-B01', 'batch-B02']
        assert first.status == second.status == 'waiting'
        assert first.result['provider_id'] == 'provider-batch-B01'
        assert second.result['provider_id'] == 'provider-batch-B02'
        assert first.lease <= time.time() + 3
