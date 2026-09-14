"""Real worker/provider/SSE path with in-memory model output and isolated storage."""
import json
import asyncio
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import select

from backend import authors, providers, worker
from backend.db import Record, Session, Task
from backend.style_progress import style_preview


def event(value):
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode()


def delta(content='', **extra):
    return event({'choices': [{'index': 0, 'delta': {'content': content, **extra}}]})


@pytest.fixture
def setup_stream(client, monkeypatch):
    for key, value in {
        'LLM_BASE_URL': 'https://models.example.test/v1', 'LLM_MODEL': 'main-model',
        'LLM_FAST_MODEL': 'style-model', 'LLM_API_KEY': 'test-key',
        'LLM_FAST_MAX_TOKENS': '2048', 'ALLOW_PAID_CALLS': 'true',
    }.items():
        monkeypatch.setenv(key, value)
    with Session.begin() as db:
        db.add(Record(id='stream-source', kind='story_source', data={'content': '故事原文'}))
        db.add(Record(id='stream-work', kind='author_project', data={'source_id': 'stream-source', 'stage': 'style'}))
    task = client.post('/api/author/projects/stream-work/recommend', json={'confirm': True}).json()
    calls = []

    def install(chunks, status=200):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield from chunks()

        @contextmanager
        def request(method, url, **kwargs):
            calls.append(kwargs['json'])
            yield httpx.Response(status, headers={'Content-Type': 'text/event-stream'}, stream=Stream())
        monkeypatch.setattr(providers.httpx, 'stream', request)
    return task['id'], install, calls


def test_real_partial_updates_before_model_finishes(client, setup_stream):
    task_id, install, calls = setup_stream
    summary = '先区分画面的质感，再让三种不同的情绪形成对照。'
    options = [{'art': '水彩手绘', 'tone': '温柔奇幻', 'reason': '色彩柔和，突出想象。'},
               {'art': '黑白漫画', 'tone': '紧张悬疑', 'reason': '用明暗强化冲突。'},
               {'art': '复古动画', 'tone': '荒诞幽默', 'reason': '强调人物反应。'}]
    for option in options:option['prompt']=option['art']+'，清晰墨线，分层明暗，低饱和色板，纸张肌理'
    snapshots = []

    def chunks():
        yield b': heartbeat\n\n'
        yield delta(reasoning_content='provider-private-reasoning')
        snapshots.append(authors.recommendation_snapshot('stream-work', task_id))
        yield delta('{"approach":' + json.dumps(summary, ensure_ascii=False) + ',"options":[')
        snapshots.append(authors.recommendation_snapshot('stream-work', task_id))
        assert 'recommendations' not in authors.workspace('stream-work')
        yield delta(json.dumps(options[0], ensure_ascii=False) + ',')
        snapshots.append(authors.recommendation_snapshot('stream-work', task_id))
        assert 'recommendations' not in authors.workspace('stream-work')
        yield delta(','.join(json.dumps(option, ensure_ascii=False) for option in options[1:]) + ']}')
        yield event({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
        yield event({'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 200, 'total_tokens': 300}})
        yield b'data: [DONE]\n\n'

    install(chunks)
    assert worker.process_one('stream-test')
    assert snapshots[0]['result']['live']['phase'] == 'reasoning'
    assert snapshots[1]['status'] == 'running'
    assert snapshots[1]['result']['live']['summary'] == summary
    assert snapshots[2]['result']['live']['options'] == [options[0]]
    assert 'provider-private-reasoning' not in json.dumps(snapshots)
    result = client.get('/api/author/projects/stream-work').json()
    assert result['recommend_task_status']['status'] == 'completed'
    assert result['recommend_task_status']['result']['live']['phase'] == 'ready'
    assert result['recommendations'] == options
    assert 'usage' not in result
    assert len(calls) == 1
    stream = client.get(f'/api/author/projects/stream-work/recommendations/{task_id}/events')
    assert stream.status_code == 200
    assert stream.headers['content-type'].startswith('text/event-stream')
    assert 'no-transform' in stream.headers['cache-control']
    assert json.loads(stream.text.removeprefix('data: ').strip())['status'] == 'completed'
    assert '故事原文' not in stream.text


@pytest.mark.parametrize('failure', ['disconnect', 'no_finish', 'invalid_schema', 'length', 'provider_error'])
def test_failed_stream_keeps_drafts_and_never_replays(client, setup_stream, failure):
    task_id, install, calls = setup_stream

    def chunks():
        yield delta('{"approach":"已收到的构思摘要","options":[')
        if failure == 'disconnect':
            raise httpx.RemoteProtocolError('closed')
        if failure == 'provider_error':
            yield event({'error': {'message': 'private upstream error'}})
            return
        if failure == 'no_finish':
            return
        yield delta(']}')
        yield event({'choices': [{'delta': {}, 'finish_reason': 'length' if failure == 'length' else 'stop'}]})
        yield b'data: [DONE]\n\n'

    install(chunks)
    assert worker.process_one('stream-test')
    result = client.get('/api/author/projects/stream-work').json()
    assert result['recommend_task_status']['status'] == 'needs_review'
    assert result['recommend_task_status']['result']['live']['summary'] == '已收到的构思摘要'
    assert 'recommendations' not in result
    assert 'private upstream error' not in result['recommend_task_status']['message']
    assert len(calls) == 1
    with Session() as db:
        entries = list(db.scalars(select(Record).where(Record.kind == 'usage')))
        assert len(entries) == 1
        if failure in ['disconnect', 'no_finish', 'provider_error']:
            assert entries[0].data['status'] == 'pending'


def test_rejected_stream_has_one_rejected_usage_record(setup_stream):
    task_id, install, calls = setup_stream
    install(lambda: iter([]), status=400)
    worker.process_one('stream-test')
    with Session() as db:
        assert db.get(Task, task_id).status == 'needs_review'
        assert db.scalar(select(Record).where(Record.kind == 'usage')).data['status'] == 'rejected'
    assert len(calls) == 1


def test_stream_does_not_accept_a_task_from_another_work(client, setup_stream):
    task_id, _, _ = setup_stream
    with Session.begin() as db:
        db.add(Record(id='other-work', kind='author_project', data={}))
    assert client.get(f'/api/author/projects/other-work/recommendations/{task_id}/events').status_code == 404


def test_stale_worker_cannot_publish_a_style_result(setup_stream):
    task_id, install, calls = setup_stream
    def chunks():
        yield delta('{"approach":"部分摘要",')
        with Session.begin() as db:
            task = db.get(Task, task_id)
            task.owner = 'new-worker'
            task.status = 'needs_review'
        yield delta('"options":[]}')
        yield event({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
    install(chunks)
    worker.process_one('stream-test')
    with Session() as db:
        assert 'recommendations' not in db.get(Record, 'stream-work').data
        assert db.get(Task, task_id).owner == 'new-worker'
    assert len(calls) == 1


def test_partial_json_handles_escaped_strings_and_arbitrary_option_key_order():
    assert style_preview('{"approach":"画风\\u6e2') == {'summary': '画风', 'options': []}
    assert style_preview('{"approach":"画风\\u6e29\\u67') == {'summary': '画风温', 'options': []}
    preview = style_preview('{"approach":"保留 \\"方向\\" 与 {差异}","options":[{"reason":"适配原因","art":"水彩","tone":"温柔')
    assert preview['summary'] == '保留 "方向" 与 {差异}'
    assert preview['options'] == [{'art': '水彩', 'tone': '温柔', 'reason': '适配原因'}]
    assert style_preview('{"options":[{"art":123,"reason":"文本"}]}')['options'] == [{'reason': '文本'}]
    assert style_preview('{"approach":"色彩\\ud83c')['summary'] == '色彩'
    assert style_preview('{"approach":"色彩\\ud83c\\udfa8')['summary'] == '色彩🎨'


def test_sse_delivers_updates_before_terminal_state(setup_stream):
    task_id, _, calls = setup_stream
    class Request:
        async def is_disconnected(self):
            return False
    async def read():
        response = await authors.recommendation_events('stream-work', task_id, Request())
        body = response.body_iterator
        first = json.loads((await anext(body)).removeprefix('data: '))
        assert first['status'] == 'queued'
        with Session.begin() as db:
            task = db.get(Task, task_id)
            task.status = 'running'
            task.result = {'live': {'phase': 'drafting', 'summary': '正在返回的摘要'}}
        middle = json.loads((await anext(body)).removeprefix('data: '))
        assert middle['status'] == 'running'
        assert middle['result']['live']['summary'] == '正在返回的摘要'
        with Session.begin() as db:
            db.get(Task, task_id).status = 'completed'
        last = json.loads((await anext(body)).removeprefix('data: '))
        assert last['status'] == 'completed'
        with pytest.raises(StopAsyncIteration):
            await anext(body)
    asyncio.run(read())
    assert not calls
