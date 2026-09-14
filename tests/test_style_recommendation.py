"""Exercise recommendations through the real provider adapter without network calls."""
import json
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import select

from backend import providers, worker
from backend.db import Record, Session, Task


@pytest.mark.parametrize('fast_model', ['style-model', ''])
def test_recommendation_worker_uses_fast_profile(client, monkeypatch, fast_model):
    for key, value in {
        'LLM_BASE_URL': 'https://models.example.test/v1',
        'LLM_MODEL': 'main-model',
        'LLM_FAST_MODEL': fast_model,
        'LLM_API_KEY': 'test-key',
        'LLM_MAX_TOKENS': '8192',
        'LLM_FAST_MAX_TOKENS': '2048',
        'ALLOW_PAID_CALLS': 'true',
    }.items():
        monkeypatch.setenv(key, value)

    source = '雨夜里，女孩发现时钟停下后，窗外的人也停止了动作。'
    options = [
        {'art': art, 'tone': tone, 'reason': reason, 'prompt': art+'，清晰墨线，分层明暗，低饱和色板，细颗粒纸张肌理'}
        for art, tone, reason in [
            ('水彩手绘', '温柔奇幻', '用流动的水彩对照凝固的时间。'),
            ('黑白漫画', '紧张悬疑', '用强烈的明暗突出雨夜的异常。'),
            ('复古赛璐璐', '荒诞幽默', '用鲜明的色块强调静止的人群。'),
        ]
    ]
    calls = []

    @contextmanager
    def stream(method, url, **kwargs):
        assert method == 'POST'
        calls.append(url)
        assert url == 'https://models.example.test/v1/chat/completions'
        body = kwargs['json']
        assert body['model'] == (fast_model or 'main-model')
        assert body['max_tokens'] == 2048
        assert body['stream'] is True
        assert body['stream_options'] == {'include_usage': True}
        assert json.loads(body['messages'][1]['content'])['source'] == source
        yield httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'options': options}, ensure_ascii=False),
            }}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 200, 'total_tokens': 300},
        })

    monkeypatch.setattr(providers.httpx, 'stream', stream)
    with Session.begin() as db:
        db.add(Record(id='style-source', kind='story_source', data={'content': source}))
        db.add(Record(id='style-work', kind='author_project', data={
            'source_id': 'style-source', 'stage': 'style',
        }))

    path = '/api/author/projects/style-work'
    response = client.post(path + '/recommend', json={'confirm': True})
    assert response.status_code == 200, response.text
    task_id = response.json()['id']
    assert client.post(path + '/recommend', json={'confirm': True}).json()['id'] == task_id
    assert calls == []  # Enqueueing does not submit the model request.

    assert worker.process_one('style-test')
    workspace = client.get(path).json()
    assert workspace['recommend_task_status']['status'] == 'completed'
    assert workspace['recommendations'] == options
    assert workspace['stage'] == 'style'
    assert workspace['usage']['tokens'] == 300
    assert len(calls) == 1
    with Session() as db:
        assert len(list(db.scalars(select(Task)))) == 1
        usage = list(db.scalars(select(Record).where(Record.kind == 'usage')))
        assert len(usage) == 1
        assert usage[0].data['task'] == task_id
        assert usage[0].data['status'] == 'completed'
