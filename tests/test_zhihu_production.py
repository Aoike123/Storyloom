"""The login flow must work behind the production origin boundary.

The hosted deployment wraps the app in ``ProductionOriginBoundary``, which allows only the
configured public origin on unsafe methods. A login that works locally can still fail there, so
this exercises the exact production wrapping with the real site's origin.
"""
import pytest
from fastapi.testclient import TestClient

from backend import zhihu_oauth
from backend.app import app
from backend.production_boundary import ProductionOriginBoundary

PUBLIC_ORIGIN = 'https://storyloom.aoike.top'


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    monkeypatch.setenv('STORYLOOM_PUBLIC_ORIGIN', PUBLIC_ORIGIN)
    monkeypatch.setenv('ZHIHU_OAUTH_APP_ID', '525')
    monkeypatch.setenv('ZHIHU_OAUTH_APP_KEY', 'test-app-key')
    monkeypatch.setenv('ZHIHU_OAUTH_REDIRECT_URI', PUBLIC_ORIGIN + '/auth/callback')
    monkeypatch.setenv('MODEL_ACCESS_SECRET', 'test-secret-' + 'c' * 48)
    monkeypatch.setattr(zhihu_oauth, '_post_form',
                        lambda url, form: {'access_token': 'token', 'expires_in': 3600})
    monkeypatch.setattr(zhihu_oauth, '_get_profile',
                        lambda token: {'uid': 969570047710216200, 'fullname': '读者'})
    # Serve the client on the real HTTPS origin: the login cookie is Secure in public mode, exactly
    # as it is behind Caddy, and a plain-http client would not send it.
    return TestClient(ProductionOriginBoundary(app, PUBLIC_ORIGIN), base_url=PUBLIC_ORIGIN)


def test_login_and_logout_survive_the_production_origin_boundary(hosted):
    from urllib.parse import parse_qs, urlparse

    started = hosted.get('/api/zhihu/login', follow_redirects=False)
    assert started.status_code == 302
    query = parse_qs(urlparse(started.headers['location']).query)
    assert query['redirect_uri'] == [PUBLIC_ORIGIN + '/auth/callback']
    state = query['state'][0]

    # The callback is a GET navigation from Zhihu, so the origin boundary must not block it.
    returned = hosted.get(f'/auth/callback?authorization_code=c&state={state}', follow_redirects=False)
    assert returned.status_code == 302 and returned.headers['location'] == '/?zhihu=ok'
    assert hosted.get('/api/zhihu/status').json()['authorized'] is True

    # The browser's own write arrives with the public origin and must be accepted...
    accepted = hosted.post('/api/zhihu/logout', headers={'Origin': PUBLIC_ORIGIN})
    assert accepted.status_code == 204
    assert hosted.get('/api/zhihu/status').json()['authorized'] is False


def test_a_foreign_origin_cannot_write(hosted):
    assert hosted.post('/api/zhihu/logout', headers={'Origin': 'https://evil.example.com'}).status_code == 403
