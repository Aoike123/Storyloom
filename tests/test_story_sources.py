"""Local story import: paste or upload text and it becomes an immutable story_source."""
import hashlib

from sqlalchemy import select

from backend.db import Record, Session

CONTENT = '在雨夜里，灯塔守塔人发现光并非来自灯，而是来自海面。'


def _import(client, content=CONTENT, title='灯塔'):
    return client.post('/api/stories/import', json={'title': title, 'content': content})


def test_import_creates_an_immutable_source(client):
    response = _import(client)
    assert response.status_code == 200, response.text
    body = response.json()
    story = body['story']
    assert body['stale'] is False and body['warning'] is None
    assert story['id'] == 'source_' + hashlib.sha256(CONTENT.encode('utf-8')).hexdigest()[:32]
    assert story['version'] == 1
    assert story['title'] == '灯塔'
    assert story['labels'] == ['本地导入']
    assert story['source'] == '本地导入'
    assert story['status'] == 'imported'
    assert story['content'] == CONTENT


def test_same_content_imports_to_the_same_id(client):
    first = _import(client).json()['story']
    again = _import(client).json()['story']
    assert again['id'] == first['id']
    with Session() as db:
        rows = db.scalars(select(Record).where(Record.kind == 'story_source')).all()
    assert len(rows) == 1


def test_import_appears_in_the_market(client):
    _import(client)
    listing = client.get('/api/stories').json()
    assert [item['title'] for item in listing['items']] == ['灯塔']
    assert listing['items'][0]['labels'] == ['本地导入']


def test_detail_returns_the_imported_story(client):
    sid = _import(client).json()['story']['id']
    detail = client.get(f'/api/stories/{sid}').json()
    assert detail['id'] == sid
    assert detail['content'] == CONTENT
    assert detail['listing']['work_id'] == sid


def test_detail_missing_story_is_a_404(client):
    response = client.get('/api/stories/source_' + 'f' * 32)
    assert response.status_code == 404


def test_empty_content_is_rejected(client):
    response = client.post('/api/stories/import', json={'content': '   '})
    assert response.status_code == 422


def test_import_file_reads_a_utf8_file(client):
    response = client.post(
        '/api/stories/import-file',
        files={'file': ('灯塔.txt', CONTENT.encode('utf-8'), 'text/plain')},
        data={'title': '文件故事'})
    assert response.status_code == 200, response.text
    story = response.json()['story']
    assert story['title'] == '文件故事'
    assert story['content'] == CONTENT
