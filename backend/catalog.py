"""Reader catalog: all returned brainstorm stories, joined to real local releases."""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from .db import DATA, Session, Record
from . import zhihu_stories

router = APIRouter(prefix='/api/reader', tags=['reader'])


def labels(value):
    return [x.strip() for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def playable(entries):
    if not isinstance(entries, list) or not entries:
        return False
    root = (DATA / 'media').resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        media, start, end = entry.get('media'), entry.get('start'), entry.get('end')
        if not isinstance(media, str) or not media.startswith('/media/'):
            return False
        path = (root / media.removeprefix('/media/')).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return False
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not 0 <= start < end:
            return False
    return True


@router.get('/catalog')
def catalog(refresh: bool = False):
    try:
        listing = zhihu_stories.stories(refresh=refresh)
        official = [item for item in listing['items'] if '脑洞' in labels(item.get('labels'))]
        available = True
    except HTTPException as exc:
        listing = {'items': [], 'cached': False, 'stale': False, 'fetched_at': None, 'warning': exc.detail}
        official, available = [], False
    with Session() as db:
        sources = {r.id: r.data for r in db.scalars(select(Record).where(Record.kind == 'story_source'))}
        works = list(db.scalars(select(Record).where(Record.kind == 'author_project').order_by(Record.created.desc())))
        works_by_story, works_by_source = {}, {}
        for work in works:
            source_id = work.data.get('source_id')
            story_id = work.data.get('zhihu_work_id') or sources.get(source_id, {}).get('work_id')
            if story_id:
                works_by_story.setdefault(story_id, work)
            if source_id:
                works_by_source.setdefault(source_id, work)
        releases, by_story, standalone = [], {}, []
        for row in db.scalars(select(Record).where(Record.kind == 'reader_release').order_by(Record.created.desc())):
            data = row.data
            if data.get('superseded_by_run'):continue
            if not playable(data.get('entries')):
                continue
            source = sources.get(data.get('source_id'), {})
            story_id = source.get('work_id') or data.get('source_work_id')
            work = works_by_story.get(story_id) or works_by_source.get(data.get('source_id'))
            release = {'id': row.id, **{k: data.get(k) for k in ('title', 'source_title', 'author', 'description', 'entries')},
                       'source_work_id': story_id, 'project_id': work.id if work else None}
            releases.append(release)
            if story_id:
                by_story.setdefault(story_id, release)
            elif not source or '脑洞' in labels(source.get('labels')):
                standalone.append(release)
        items, seen = [], set()
        for item in official:
            story_id = item['work_id']
            if story_id in seen:
                continue
            seen.add(story_id)
            work = works_by_story.get(story_id)
            items.append({'id': story_id, 'work_id': story_id, **{k: item.get(k) for k in ('title', 'description', 'artwork', 'tab_artwork')},
                          'labels': labels(item.get('labels')), 'release': by_story.get(story_id),
                          'project_id': work.id if work else None, 'stage': work.data.get('stage') if work else None})
        # Previously released work stays watchable if the upstream list is unavailable or changes.
        extras = [r for story_id, r in by_story.items() if story_id not in seen] + standalone
        for release in extras:
            items.append({'id': 'release:' + release['id'], 'work_id': release['source_work_id'],
                          'title': release['source_title'] or release['title'], 'description': release['description'],
                          'labels': ['本地已发布'], 'release': release, 'project_id': release['project_id'], 'stage': 'published'})
    items.sort(key=lambda item: item['release'] is None)
    return {'items': items, 'releases': releases, 'count': len(items), 'brainstorm_count': len(seen),
            'ready_count': sum(item['release'] is not None for item in items), 'upstream_total': len(listing['items']),
            'catalog_available': available, **{k: listing.get(k) for k in ('cached', 'stale', 'fetched_at', 'warning')}}
