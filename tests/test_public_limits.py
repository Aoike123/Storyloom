import pytest

from backend.db import Session, Task, TaskCapacityError
from backend.public_limits import clear_request_limit_state


def test_public_write_requests_are_limited_per_client(client, monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    monkeypatch.setenv('PUBLIC_WRITE_REQUESTS_PER_MINUTE', '1')
    clear_request_limit_state()
    try:
        first = client.post('/api/settings', json={'values': {}})
        limited = client.post('/api/settings', json={'values': {}})
    finally:
        clear_request_limit_state()

    assert first.status_code == 404
    assert limited.status_code == 429
    assert int(limited.headers['retry-after']) >= 1


def test_health_check_is_exempt_from_public_read_limit(client, monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    monkeypatch.setenv('PUBLIC_READ_REQUESTS_PER_MINUTE', '1')
    clear_request_limit_state()
    try:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/health').status_code == 200
    finally:
        clear_request_limit_state()


def test_public_active_task_capacity_is_enforced(monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    monkeypatch.setenv('PUBLIC_MAX_ACTIVE_TASKS', '1')
    with Session.begin() as db:
        db.add(Task(id='first-active', kind='image'))

    with pytest.raises(TaskCapacityError, match='生成队列已满'):
        with Session.begin() as db:
            db.add(Task(id='second-active', kind='video'))
