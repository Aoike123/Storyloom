"""Reader catalog: all returned brainstorm stories, joined to real local releases."""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from .db import DATA, Session, Record
from . import zhihu_stories
from .model_access import current_actor_id, public_demo_mode

router = APIRouter(prefix='/api/reader', tags=['reader'])


def labels(value):
    return [x.strip() for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def public_creator(value):
    """Return only the public creator fields that the market card needs."""
    source = value if isinstance(value, dict) else {}
    name = str(source.get('name') or '').strip()[:80] or '创作者'
    avatar = source.get('avatar_path')
    if not isinstance(avatar, str) or not avatar.strip().startswith('https://'):
        avatar = None
    else:
        avatar = avatar.strip()
    return {'name': name, 'avatar_path': avatar}


def release_progress(data):
    """How much of the cut this release covers: 3 of 6 episodes, for the market card."""
    units=data.get('published_units') or []
    total=int(data.get('total_units') or 0)
    if not total:return None
    return {'published_units': len(units), 'total_units': total,
            'complete': len(units) >= total, 'next_unit': data.get('next_unit')}


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
        all_works = list(db.scalars(select(Record).where(Record.kind == 'author_project').order_by(Record.created.desc())))
        actor = current_actor_id()
        # The catalogue is public, but studio state is not. Only this account's project id and stage
        # may decorate a public story; an anonymous visitor sees no private project metadata.
        works = [work for work in all_works
                 if not public_demo_mode() or work.data.get('owner') == actor]
        works_by_story, works_by_director, works_by_release = {}, {}, {}
        for work in works:
            source_id = work.data.get('source_id')
            story_id = work.data.get('zhihu_work_id') or sources.get(source_id, {}).get('work_id')
            if story_id:
                works_by_story.setdefault(story_id, work)
            if work.data.get('director_id'):
                works_by_director.setdefault(work.data['director_id'], work)
            if work.data.get('release_id'):
                works_by_release.setdefault(work.data['release_id'], work)
        releases, by_story, standalone = [], {}, []
        for row in db.scalars(select(Record).where(Record.kind == 'reader_release').order_by(Record.created.desc())):
            data = row.data
            if data.get('superseded_by_run'):continue
            if not playable(data.get('entries')):
                continue
            source = sources.get(data.get('source_id'), {})
            story_id = source.get('work_id') or data.get('source_work_id')
            # Link a release only to this account's exact publishing project. Falling back to any
            # project for the same source lets one account enter another maker's workspace.
            work = works_by_release.get(row.id) or works_by_director.get(data.get('director_id'))
            release = {'id': row.id, **{k: data.get(k) for k in ('title', 'source_title', 'author', 'description', 'entries')},
                       'source_work_id': story_id, 'project_id': work.id if work else None,
                       # Publishing progress is public: a reader deciding what to open should see
                       # how much of the cut is watchable, and the maker sees the same numbers.
                       'progress': release_progress(data),
                       'mine': bool(work), 'created': row.created, 'creator': public_creator(data.get('creator'))}
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
