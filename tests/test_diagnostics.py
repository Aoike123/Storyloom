"""Failure reports stay useful locally and stay hidden in public demo mode."""
import pytest
from fastapi import HTTPException

from backend import diagnostics
from backend.db import Record, Session, Task


def test_failure_report_keeps_a_short_code_and_the_real_cause(client):
    with Session.begin() as db:
        db.add(Task(id='broken-task', kind='image', status='running', owner='w'))
    try:
        raise TypeError("can only concatenate str (not \"NoneType\") to str")
    except TypeError as exc:
        code = diagnostics.record_failure('broken-task', exc, stage='rendering', context={'shot': 'S01'})
    assert code.startswith('E-') and len(code) == 8
    response = client.get('/api/diagnostics/' + code)
    assert response.status_code == 200
    report = response.json()
    assert report['exception'] == 'TypeError'
    assert report['stage'] == 'rendering' and report['context'] == {'shot': 'S01'}
    assert 'TypeError' in report['traceback']
    assert client.get('/api/diagnostics/E-000000').status_code == 404
    assert client.get('/api/diagnostics/not-a-code').status_code == 404


def test_secrets_are_scrubbed_from_a_failure_report():
    text = 'Authorization: Bearer sk-abcdef1234567890 api_key="abcd1234efgh5678"'
    scrubbed = diagnostics.scrub(text)
    assert 'sk-abcdef1234567890' not in scrubbed
    assert 'abcd1234efgh5678' not in scrubbed
    assert 'redacted' in scrubbed


def test_public_demo_mode_withholds_failure_details(client, monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    with Session.begin() as db:
        db.add(Task(id='broken-task', kind='image', status='running', owner='w'))
    try:
        raise TypeError('boom')
    except TypeError as exc:
        code = diagnostics.record_failure('broken-task', exc)
    assert client.get('/api/diagnostics/' + code).status_code == 404
