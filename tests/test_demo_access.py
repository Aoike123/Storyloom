from fastapi.testclient import TestClient
from sqlalchemy import select
from backend.app import app
from backend.db import DATA, Session, Record, Task, init_db


def test_local_visitors_can_use_current_apis():
    with TestClient(app) as visitor:
        for path in ['/api/health', '/api/platform', '/api/settings',
                     '/api/author/projects', '/api/reader/stories']:
            response = visitor.get(path)
            assert response.status_code == 200, path
            assert 'set-cookie' not in response.headers
        assert not visitor.cookies


def test_legacy_demo_routes_and_seed_are_removed(client):
    for path in ['/api/bootstrap', '/api/tasks', '/api/billing']:
        assert client.get(path).status_code == 404
    for path in ['/api/sessions', '/api/assets', '/api/image', '/api/video']:
        assert client.post(path, json={}).status_code == 404
    with Session() as db:
        assert db.get(Record, 'story_demo') is None
        assert db.get(Record, 'paid_budget') is None


def test_account_endpoints_are_removed():
    with TestClient(app) as visitor:
        for prefix, actions in [('/api/admin-auth', ['login', 'logout']),
                                ('/api/author', ['register', 'login', 'logout'])]:
            for action in actions:
                assert visitor.post(prefix + '/' + action, json={}).status_code == 404
            assert visitor.get(prefix + '/session').status_code == 404


def test_legacy_account_cleanup_preserves_content_and_task_leases():
    (DATA / 'admin-access.txt').write_text('obsolete-demo-password', encoding='utf-8')
    media = DATA / 'media' / 'legacy.png'
    media.write_bytes(b'keep-existing-image')
    with Session.begin() as db:
        db.add_all([
            Record(id='legacy-account', kind='author_account', data={'password': 'obsolete-hash'}),
            Record(id='legacy-login', kind='author_session', data={'author_id': 'legacy-account'}),
            Record(id='legacy-work', kind='author_project', data={'owner': 'legacy-account', 'stage': 'style', 'source_id': 'legacy-source'}),
            Record(id='legacy-source', kind='story_source', data={'owner': 'legacy-account', 'author_name': '原作署名', 'content': '保留原文'}),
            Record(id='legacy-asset', kind='asset', data={'scope': 'private', 'media': '/media/legacy.png'}),
            Task(id='legacy-task', kind='image', owner='worker-lease', status='completed'),
        ])
    init_db()
    init_db()
    with Session() as db:
        assert not list(db.scalars(select(Record).where(Record.kind.in_(['author_account', 'author_session']))))
        assert db.get(Record, 'legacy-work').data == {'stage': 'style', 'source_id': 'legacy-source'}
        assert db.get(Record, 'legacy-source').data == {'author_name': '原作署名', 'content': '保留原文'}
        assert db.get(Record, 'legacy-asset').data == {'media': '/media/legacy.png'}
        assert db.get(Task, 'legacy-task').owner == 'worker-lease'
    assert not (DATA / 'admin-access.txt').exists()
    assert media.read_bytes() == b'keep-existing-image'


def test_reader_shelf_still_requires_explicit_publication():
    with Session.begin() as db:
        db.add(Record(id='draft', kind='author_project', data={'title': '草稿', 'stage': 'style'}))
        db.add(Record(id='released', kind='reader_release', data={'title': '已发布', 'entries': []}))
    with TestClient(app) as visitor:
        assert [s['id'] for s in visitor.get('/api/reader/stories').json()] == ['released']
        assert visitor.get('/api/author/projects/draft').status_code == 200
        assert visitor.get('/api/platform').json()['published_count'] == 1


def test_platform_progress_groups_real_tasks_by_work(client):
    with Session.begin() as db:
        db.add_all([
            Record(id='work-one', kind='author_project', data={'director_id': 'director-one', 'recommend_task': 'style-one', 'stage': 'producing'}),
            Record(id='work-two', kind='author_project', data={'director_id': 'director-two', 'stage': 'preparing'}),
            Task(id='style-one', kind='author_styles', status='completed', progress=100),
            Task(id='flow-one', kind='author_flow', payload={'work_id': 'work-one'}, status='waiting', progress=30),
            Task(id='image-one', kind='image', payload={'creative_id': 'director-one'}, status='running', progress=20),
            Task(id='image-two', kind='image', payload={'preproduction_id': 'director-two'}, status='failed'),
            Task(id='legacy', kind='director', status='needs_review'),
        ])
    data = client.get('/api/platform').json()
    tasks = {t['id']: t for t in data['tasks']}
    assert tasks['style-one']['work_id'] == 'work-one'
    assert tasks['flow-one']['work_id'] == 'work-one'
    assert tasks['image-one']['work_id'] == 'work-one'
    assert tasks['image-one']['progress'] == 20
    assert tasks['image-one']['status'] == 'running'
    assert tasks['image-two']['work_id'] == 'work-two'
    assert tasks['legacy']['work_id'] is None
    assert all('payload' not in t for t in tasks.values())
