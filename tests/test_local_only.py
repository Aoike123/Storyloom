"""The workbench is purely local: no accounts, no sessions, no cookies.

These tests pin that the local endpoints respond directly and never issue a session cookie — a
cookie appearing anywhere would mean an identity concept has crept back in.
"""

LOCAL_GET_ROUTES = (
    '/api/health', '/api/platform', '/api/node-skills',
    '/api/stories', '/api/reader/catalog', '/api/author/projects', '/api/models',
)


def test_local_routes_respond_without_issuing_a_session(client):
    for path in LOCAL_GET_ROUTES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'set-cookie' not in response.headers, path
    assert not client.cookies


def test_model_status_is_read_only_and_key_free(client):
    payload = client.get('/api/models').json()
    assert set(payload) == {'llm_configured', 'image_configured', 'video_configured', 'models', 'config_file'}
    assert {'llm', 'image', 'video'} <= set(payload['models'])
    assert 'key' not in __import__('json').dumps(payload).lower()
