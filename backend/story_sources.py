"""Local story sources: the starting material for the workbench.

A story is imported locally — pasted as text or uploaded as a file — and stored as an immutable
``story_source`` record. There is no upstream content API to poll or cache: the story market simply
lists whatever has been imported, and opening one seeds an author project. The same content always
maps to the same id, so a project never silently follows changed source text.
"""
import hashlib
import time

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from sqlalchemy import select

from .db import Session, Record, record_dict

router = APIRouter(prefix='/api/stories', tags=['stories'])

MAX_CONTENT_BYTES = 2 * 1024 * 1024  # 2MB of source text
MAX_FILE_BYTES = 4 * 1024 * 1024  # 4MB uploaded file
DESCRIPTION_LIMIT = 200


def valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 120 and not any(c in value for c in '/?#\r\n') and not any(ord(c) < 32 for c in value)


def _source_id(content):
    return 'source_' + hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]


def _check_content(content):
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(422, '正文不能为空。')
    if len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
        raise HTTPException(422, '正文超过 2MB 限制，请缩短后再导入。')


def import_story(title, content, labels=None, author_name=None, artwork=None):
    """Create or refresh an immutable local source. Returns the stored record plus import flags."""
    _check_content(content)
    title = (title or '').strip()[:200] or '未命名故事'
    cleaned_labels = [x.strip() for x in (labels or []) if isinstance(x, str) and x.strip()]
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    sid = _source_id(content)
    data = {
        'work_id': sid,
        'title': title,
        'author_name': (author_name or '').strip() or None,
        'labels': cleaned_labels or ['本地导入'],
        'content': content,
        'source': '本地导入',
        'source_url': None,
        'fetched_at': time.time(),
        'content_hash': digest,
        'completeness': 'full',
        'status': 'imported',
        **({'artwork': artwork} if isinstance(artwork, str) and artwork.startswith('/media/') else {}),
    }
    with Session.begin() as db:
        row = db.get(Record, sid)
        if row:
            # Same content keeps its id; only non-content metadata may be refreshed.
            row.data = {**row.data, 'title': title, 'author_name': data['author_name'],
                        'labels': data['labels'], **({'artwork': artwork} if 'artwork' in data else {})}
            row.version += 1
        else:
            row = Record(id=sid, kind='story_source', data=data)
            db.add(row)
    return {'story': record_dict(row), 'stale': False, 'warning': None}


def stories(refresh=False):
    """Every imported source, newest first, shaped for the story market."""
    items = []
    with Session() as db:
        rows = db.scalars(select(Record).where(Record.kind == 'story_source').order_by(Record.created.desc())).all()
        for row in rows:
            d = row.data
            content = d.get('content') or ''
            items.append({
                'work_id': row.id,
                'id': row.id,
                'title': d.get('title') or '未命名故事',
                'description': (d.get('description') or content)[:DESCRIPTION_LIMIT],
                'labels': [x for x in (d.get('labels') or []) if isinstance(x, str)],
                'author_name': d.get('author_name'),
                'artwork': d.get('artwork'),
                'tab_artwork': d.get('artwork'),
            })
    return {'items': items, 'cached': True, 'stale': False, 'fetched_at': None, 'warning': None}


def detail(work_id):
    if not valid_id(work_id):
        raise HTTPException(422, '故事标识格式无效。')
    with Session() as db:
        row = db.get(Record, work_id)
        if not row or row.kind != 'story_source':
            raise HTTPException(404, '请从本地故事库选择作品。')
        data = dict(row.data)
    listing = {'work_id': row.id, 'title': data.get('title'), 'labels': data.get('labels') or []}
    return {**data, 'id': row.id, 'cached': True, 'stale': False, 'warning': None,
            'listing': listing, 'completeness': data.get('completeness', 'full')}


@router.post('/import')
def import_text(body: dict):
    """Paste a micro-fiction straight into the local library."""
    title = body.get('title') if isinstance(body, dict) else None
    content = body.get('content') if isinstance(body, dict) else None
    labels = body.get('labels') if isinstance(body, dict) else None
    author_name = body.get('author_name') if isinstance(body, dict) else None
    artwork = body.get('artwork') if isinstance(body, dict) else None
    return import_story(title, content, labels=labels, author_name=author_name, artwork=artwork)


@router.post('/import-file')
async def import_file(file: UploadFile = File(...), title: str | None = Form(None)):
    """Upload a text file (txt/md) into the local library."""
    raw = await file.read()
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(422, '文件超过 4MB 限制，请压缩后再导入。')
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(422, '文件必须是 UTF-8 文本。') from None
    fallback = (file.filename or '').rsplit('.', 1)[0].strip()
    return import_story(title or fallback or None, content, labels=['本地导入'])


@router.get('')
def list_stories(refresh: bool = False):
    return stories(refresh=refresh)


@router.get('/imports')
def imports():
    with Session() as db:
        return [record_dict(r) for r in db.scalars(select(Record).where(Record.kind == 'story_source'))]


@router.get('/{work_id}')
def story_detail(work_id: str, refresh: bool = False):
    return detail(work_id)
