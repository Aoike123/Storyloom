"""The workbench is purely local: no accounts, no sessions, no cookies.

These tests pin that the local endpoints respond directly and never issue a session cookie — a
cookie appearing anywhere would mean an identity concept has crept back in.
"""

LOCAL_GET_ROUTES = (
    '/api/health', '/api/platform', '/api/node-skills',
    '/api/stories', '/api/reader/catalog', '/api/author/projects',
)


def test_local_routes_respond_without_issuing_a_session(client):
    for path in LOCAL_GET_ROUTES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'set-cookie' not in response.headers, path
    assert not client.cookies
