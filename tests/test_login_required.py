"""Nothing may spend the operator's keys without a payer.

A signed-in account's beans, or the visitor's own keys, are the only two ways to start a paid call.
This drives every entry that would submit one, with no session at all, and requires each to be
refused. A front-end redirect is not a control, so the check lives in the server.
"""
import pytest
from fastapi.testclient import TestClient

from backend import model_access
from backend.app import app
from backend.db import Record, Session, Task


@pytest.fixture
def anonymous(monkeypatch):
    """A public deployment, fully configured, with a browser that has no session of any kind."""
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    monkeypatch.setenv('LLM_API_KEY', 'operator-llm-key')
    monkeypatch.setenv('IMAGE_API_KEY', 'operator-image-key')
    monkeypatch.setenv('VIDEO_API_KEY', 'operator-video-key')
    monkeypatch.setenv('LLM_MODEL', 'deepseek-flash')
    monkeypatch.setenv('LLM_FAST_MODEL', 'deepseek-flash')
    return TestClient(app)


def test_every_paid_entry_refuses_an_anonymous_browser(anonymous):
    client = anonymous
    attempts = [
        ('post', '/api/author/projects/p/start', {'art': '手绘漫画', 'tone': '温馨', 'confirm_paid': True}),
        ('post', '/api/author/projects/p/generate', {'confirm': True, 'confirm_paid': True}),
        ('post', '/api/author/projects/p/retry-node', {'confirm_paid': True}),
        ('post', '/api/author/projects/p/recommend', {'confirm': True}),
        ('post', '/api/author/projects/p/images/t/retry', {'confirm_paid': True}),
        ('post', '/api/creative/pid/design', {'art': '手绘漫画', 'tone': '温馨', 'confirm_paid': True}),
        ('post', '/api/creative/pid/continue', {'stage': 'samples_review', 'confirm_review': True, 'confirm_paid': True}),
        ('post', '/api/creative/pid/feedback', {'task_id': 't', 'text': '改一下', 'confirm_paid': True}),
        ('post', '/api/preproduction/pid/trial',
         {'stamp': 's', 'assets': ['a', 'b'], 'prompt': '已完成定装的试拍请求', 'confirm_paid': True}),
        ('post', '/api/director/projects/pid/storyboard', {'version': 1, 'confirm_paid': True}),
        ('post', '/api/director', {'source_id': 's', 'confirm_paid': True}),
        ('post', '/api/production/pid/shots/S01/video', {'version': 1, 'confirm_paid': True}),
        ('post', '/api/reader/branches',
         {'release_id': 'r', 'index': 0, 'offset': 0, 'text': '换一个选择', 'confirm_generation': True},
         {'X-Reader-Session': 'reader-session-value-1234'}),
    ]
    for attempt in attempts:
        method, path, body, *rest = attempt
        response = getattr(client, method)(path, json=body, headers=rest[0] if rest else None)
        # Any refusal is fine here; what matters is that it never proceeds. The next test proves
        # nothing was queued as a result.
        assert response.status_code >= 400, f'{path} -> {response.status_code}'
        body_text = response.text
        refusal = ('Key', '登录', '不存在', '共享体验池', '未开启')
        assert any(word in body_text for word in refusal), f'{path} -> {body_text[:160]}'


def test_no_task_is_created_by_those_attempts(anonymous):
    """Refusing is not enough: nothing may be queued that a worker would later submit."""
    client = anonymous
    client.post('/api/creative/pid/design', json={'art': '手绘漫画', 'tone': '温馨', 'confirm_paid': True})
    client.post('/api/production/pid/shots/S01/video', json={'version': 1, 'confirm_paid': True})
    client.post('/api/author/projects/p/start', json={'art': '手绘漫画', 'tone': '温馨', 'confirm_paid': True})
    with Session() as db:
        assert list(db.query(Task).all()) == []


def test_the_two_real_ways_forward_are_open(anonymous, monkeypatch):
    """The gate must refuse exactly when there is no payer, and not when there is one."""
    client = anonymous
    # Own keys are accepted and become a payer.
    monkeypatch.setenv('MODEL_ACCESS_SECRET', 'test-secret-' + 'd' * 48)
    session = client.post('/api/model-access/sessions',
                          json={'mode': 'own', 'keys': {'deepseek': 'visitor-key-value'}})
    assert session.status_code == 200
    with model_access.access_scope(model_access.resolve_access_token(session.json()['token'])):
        assert model_access.has_payer() is True
        assert model_access.authorize_call('llm')['mode'] == 'own'

    # A signed-in account is a payer too.
    created = model_access.create_account_session('session-paid', '525')
    with model_access.access_scope(created['access_id']):
        assert model_access.has_payer() is True
        assert model_access.authorize_call('llm', task_id='t')['mode'] == 'account'


def test_a_stale_public_pool_session_cannot_pay(anonymous):
    """Sessions created before the pool was removed must not let a browser start a paid call."""
    with Session.begin() as db:
        db.add(Record(id='model_access_' + 'f' * 64, kind=model_access.SESSION_KIND,
                      data={'mode': 'public', 'expires_at': 4102444800}))
    with model_access.access_scope('model_access_' + 'f' * 64):
        assert model_access.has_payer() is False
        assert model_access.access_paid_states() == {'all': False, 'llm': False, 'image': False, 'video': False}
        with pytest.raises(model_access.ModelAccessError, match='登录'):
            model_access.authorize_call('llm')


def test_the_provider_boundary_is_the_last_line_of_defence(anonymous):
    """Even if a caller forgot its own check, submitting must fail before any network call."""
    from backend import providers
    with pytest.raises(providers.ProviderError, match='登录'):
        providers.reserve_call('video', 'task', duration_seconds=8)


def test_the_status_endpoint_tells_the_truth_about_this_browser(anonymous, monkeypatch):
    """The interface must not have to guess from local storage whether a payer exists."""
    client = anonymous
    assert client.get('/api/zhihu/status').json()['can_generate'] is False

    monkeypatch.setenv('MODEL_ACCESS_SECRET', 'test-secret-' + 'd' * 48)
    session = client.post('/api/model-access/sessions',
                          json={'mode': 'own', 'keys': {'deepseek': 'visitor-key-value'}}).json()
    with model_access.access_scope(model_access.resolve_access_token(session['token'])):
        assert model_access.has_payer() is True

    created = model_access.create_account_session('session-truth', '525')
    with model_access.access_scope(created['access_id']):
        assert model_access.has_payer() is True
