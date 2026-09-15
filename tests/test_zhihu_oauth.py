"""Zhihu account login: state validation, token handling and account-key safety."""
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend import zhihu_oauth
from backend.db import Record, Session, uid
from backend.app import app

APP_KEY = 'test-app-key-should-never-appear-in-output'
REDIRECT = 'https://demo.example.com/auth/callback'


@pytest.fixture
def zhihu(monkeypatch):
    monkeypatch.setenv('ZHIHU_OAUTH_APP_ID', '525')
    monkeypatch.setenv('ZHIHU_OAUTH_APP_KEY', APP_KEY)
    monkeypatch.setenv('ZHIHU_OAUTH_REDIRECT_URI', REDIRECT)
    monkeypatch.setenv('MODEL_ACCESS_SECRET', 'test-secret-' + 'b' * 48)
    return monkeypatch


def fake_profile(uid_value):
    return lambda token: {'uid': uid_value, 'hash_id': 'abc123', 'fullname': '测试读者',
                          'avatar_path': 'https://picx.zhimg.com/a.jpg', 'headline': '一句话介绍'}


def fake_token(payload=None):
    return lambda url, form: (payload or {'access_token': 'oauth-token-value',
                                          'token_type': 'Bearer', 'expires_in': 3600})


def begin_login(client, next=None):
    target = '/api/zhihu/login' + (f'?next={next}' if next else '')
    response = client.get(target, follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers['location']).query)
    assert query['app_id'] == ['525']
    assert query['redirect_uri'] == [REDIRECT]
    assert query['response_type'] == ['code']
    assert APP_KEY not in response.headers['location']
    return query['state'][0]


def test_login_reports_configuration_state(client, monkeypatch):
    monkeypatch.delenv('ZHIHU_OAUTH_APP_ID', raising=False)
    monkeypatch.delenv('ZHIHU_OAUTH_APP_KEY', raising=False)
    monkeypatch.delenv('ZHIHU_OAUTH_REDIRECT_URI', raising=False)
    body = client.get('/api/zhihu/status').json()
    assert body == {'configured': False, 'authorized': False, 'account': None, 'wallet': None}
    assert client.get('/api/zhihu/login', follow_redirects=False).status_code == 409


def test_successful_login_stores_account_and_keeps_the_token_server_side(client, zhihu):
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', fake_profile(969570047710216200))
    state = begin_login(client)
    response = client.get(f'/api/zhihu/callback?authorization_code=code-1&state={state}',
                          follow_redirects=False)
    assert response.status_code == 302 and response.headers['location'] == '/?zhihu=ok'

    status = client.get('/api/zhihu/status').json()
    assert status['authorized'] is True
    assert status['account']['uid'] == '969570047710216200'
    assert status['account']['fullname'] == '测试读者'
    assert 'token' not in json.dumps(status)

    session_id = client.cookies.get('storyloom_zhihu_session')
    with Session() as db:
        row = db.get(Record, zhihu_oauth._session_record_id(session_id))
        assert row.kind == zhihu_oauth.SESSION_KIND
        assert row.data['uid'] == '969570047710216200'
        # The token is encrypted at rest, so the raw value never appears in the stored record.
        assert 'oauth-token-value' not in json.dumps(row.data)
        assert zhihu_oauth.open_secret(row.data['token']) == 'oauth-token-value'


def test_large_int64_uid_is_kept_exactly_as_text(client, zhihu):
    """`uid` exceeds JavaScript's safe integer range, so two people could otherwise collide."""
    precise = 9007199254740993
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', fake_profile(precise))
    state = begin_login(client)
    client.get(f'/api/zhihu/callback?authorization_code=c&state={state}', follow_redirects=False)
    assert client.get('/api/zhihu/status').json()['account']['uid'] == str(precise)


def test_callback_rejects_missing_unknown_reused_and_expired_state(client, zhihu):
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', fake_profile(525))
    client.get('/api/zhihu/login', follow_redirects=False)
    assert client.get('/api/zhihu/callback?authorization_code=c', follow_redirects=False).headers['location'] == '/?zhihu=error'

    unknown = 'x' * 32
    assert client.get(f'/api/zhihu/callback?authorization_code=c&state={unknown}',
                      follow_redirects=False).headers['location'] == '/?zhihu=error'

    state = begin_login(client)
    assert client.get(f'/api/zhihu/callback?authorization_code=c&state={state}',
                      follow_redirects=False).headers['location'] == '/?zhihu=ok'
    # Replaying the same callback must fail: the value was consumed once.
    assert client.get(f'/api/zhihu/callback?authorization_code=c&state={state}',
                      follow_redirects=False).headers['location'] == '/?zhihu=error'

    expired = 'y' * 32
    with Session.begin() as db:
        db.add(Record(id=zhihu_oauth._state_record_id(expired), kind=zhihu_oauth.STATE_KIND,
                      data={'session_id': client.cookies.get('storyloom_zhihu_session'),
                            'created_at': time.time() - 1000, 'expires_at': time.time() - 10}))
    assert client.get(f'/api/zhihu/callback?authorization_code=c&state={expired}',
                      follow_redirects=False).headers['location'] == '/?zhihu=error'


def test_state_is_bound_to_the_browser_that_started_the_login(client, zhihu):
    state = begin_login(client)
    other = TestClient(app)
    other.get('/api/zhihu/login', follow_redirects=False)
    response = other.get(f'/api/zhihu/callback?authorization_code=c&state={state}',
                         follow_redirects=False)
    assert response.headers['location'] == '/?zhihu=error'
    assert other.get('/api/zhihu/status').json()['authorized'] is False


def test_accepts_legacy_code_parameter_and_denied_authorization(client, zhihu):
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', fake_profile(525))
    state = begin_login(client)
    # Older documentation used `code`; the receiver accepts both spellings.
    assert client.get(f'/api/zhihu/callback?code=c&state={state}',
                      follow_redirects=False).headers['location'] == '/?zhihu=ok'
    assert client.get('/api/zhihu/callback?error=access_denied',
                      follow_redirects=False).headers['location'] == '/?zhihu=denied'


def test_missing_user_id_fails_instead_of_inventing_one(client, zhihu):
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', lambda token: {'fullname': '无名'})
    state = begin_login(client)
    response = client.get(f'/api/zhihu/callback?authorization_code=c&state={state}',
                          follow_redirects=False)
    assert response.headers['location'] == '/?zhihu=error'
    assert client.get('/api/zhihu/status').json()['authorized'] is False
    # The reason is kept server-side for the operator instead of being shown to the visitor.
    from sqlalchemy import select
    with Session() as db:
        rows = list(db.scalars(select(Record).where(Record.kind == 'audit')))
        assert any('用户标识' in str(row.data.get('reason')) for row in rows)


def test_network_failure_during_token_exchange_does_not_crash_the_callback(client, zhihu):
    def broken(url, form):
        raise zhihu_oauth.HTTPException(502, '连接知乎令牌接口失败，未自动重试，请重新登录。')

    zhihu.setattr(zhihu_oauth, '_post_form', broken)
    state = begin_login(client)
    response = client.get(f'/api/zhihu/callback?authorization_code=c&state={state}',
                          follow_redirects=False)
    assert response.status_code == 302 and response.headers['location'] == '/?zhihu=error'
    assert client.get('/api/zhihu/status').json()['authorized'] is False


def test_app_key_never_reaches_any_browser_facing_endpoint(client, zhihu):
    """The deployment key must stay out of settings, status and login responses."""
    for path in ('/api/settings', '/api/zhihu/status', '/api/platform'):
        body = client.get(path)
        assert APP_KEY not in body.text, path
    with Session() as db:
        for row in db.query(Record).all():
            assert APP_KEY not in json.dumps(row.data), row.id


def test_public_auth_callback_path_works_like_the_api_route(client, zhihu):
    """The registered public path must reach the same handler as the internal route."""
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', fake_profile(525))
    state = begin_login(client)
    response = client.get(f'/auth/callback?authorization_code=c&state={state}', follow_redirects=False)
    assert response.status_code == 302 and response.headers['location'] == '/?zhihu=ok'
    assert client.get('/api/zhihu/status').json()['authorized'] is True
    # No code at all still lands on the page with an error flag rather than an exception.
    assert client.get('/auth/callback', follow_redirects=False).headers['location'] == '/?zhihu=error'


def test_login_destination_is_same_site_only(client, zhihu):
    """`next` must never turn the callback into an open redirect."""
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', fake_profile(525))

    state = begin_login(client, next='/author')
    assert client.get(f'/api/zhihu/callback?authorization_code=c&state={state}',
                      follow_redirects=False).headers['location'] == '/author?zhihu=ok'

    for hostile in ('https://evil.example.com', '//evil.example.com', 'javascript:alert(1)'):
        client.post('/api/zhihu/logout')
        state = begin_login(client, next=hostile)
        location = client.get(f'/api/zhihu/callback?authorization_code=c&state={state}',
                              follow_redirects=False).headers['location']
        assert location == '/?zhihu=ok', hostile


def test_logout_drops_the_session_and_its_token(client, zhihu):
    zhihu.setattr(zhihu_oauth, '_post_form', fake_token())
    zhihu.setattr(zhihu_oauth, '_get_profile', fake_profile(525))
    state = begin_login(client)
    client.get(f'/api/zhihu/callback?authorization_code=c&state={state}', follow_redirects=False)
    session_id = client.cookies.get('storyloom_zhihu_session')
    assert client.post('/api/zhihu/logout').status_code == 204
    assert client.get('/api/zhihu/status').json()['authorized'] is False
    with Session() as db:
        assert db.get(Record, zhihu_oauth._session_record_id(session_id)) is None
