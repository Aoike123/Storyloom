"""Cut a finished work out of one deployment and load it into another.

The public demo needs a real, watchable film before a single model call is paid for, and it needs it
again after every production reset. This tool carves one work out of a database that already made it
— story, reference pictures, cut, boards, clips and the published release — copies exactly the media
those records point at, and loads the same bundle into another deployment.

Only a clean path is exported: completed work, the records it depends on, and the media they name.
Failed and superseded attempts, billing entries and audit noise are dropped, because a demo shows the
finished journey rather than the debugging behind it.

Run the export where the finished work lives, and the import inside the target deployment:

    python scripts/demo-data.py export --out ../demo-data
    python scripts/demo-data.py import ../demo-data            # gives it to every account known here
    python scripts/demo-data.py accounts                       # which accounts those are

Each account receives its own project record pointing at the same finished film, because the studio
only lists the work that belongs to the account asking. ``assign`` does that step on its own, so a
deployment that imported the bundle before anybody signed in can hand it to the accounts that exist
later without copying 114 MB of media again.

The import is idempotent: records that already exist are left alone, and files are only copied when
their bytes differ. It never deletes anything, so it is safe on a deployment that already has data.
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Records that describe the work itself. Anything else in the database is either history we do not
# want (usage, audits, failure reports) or belongs to another work.
RECORD_KINDS = (
    'story_source', 'asset', 'asset_revision', 'shot_revision', 'node_output',
    'preproduction', 'preproduction_gate', 'visual_config', 'creative_run',
    'director', 'clip', 'clip_artifact', 'clip_use', 'reader_release', 'author_project',
)
# Coordinators and media producers keep the studio's progress panel readable.
TASK_KINDS = (
    'art_design', 'author_styles', 'director', 'creative_watch', 'image', 'video',
    'author_flow', 'author_storyboard', 'author_render',
)
STATUSES = ('completed',)
MEDIA = re.compile(r'/media/[^"\s\\]+')


def rel(path):
    return path.resolve().relative_to(ROOT).as_posix()


def media_file(url):
    """A '/media/...' URL is served from data/media, so the file lives under that same prefix."""
    from backend.db import DATA

    return (DATA / 'media' / url.removeprefix('/media/')).resolve()


def known_accounts(db):
    """Every real Zhihu account this deployment has seen, most recent login first."""
    from backend.db import Record
    from backend.zhihu_oauth import SESSION_KIND

    accounts = {}
    for row in db.query(Record).filter(Record.kind == SESSION_KIND).all():
        data = row.data or {}
        uid = str(data.get('uid') or '').strip()
        if not uid:
            continue
        seen = float(data.get('expires_at') or 0)
        if uid in accounts and accounts[uid]['expires_at'] >= seen:
            continue
        accounts[uid] = {'uid': uid, 'actor': 'account:' + uid, 'expires_at': seen,
                         'name': str(data.get('fullname') or '').strip() or None,
                         'avatar': data.get('avatar_path')}
    return sorted(accounts.values(), key=lambda account: -account['expires_at'])


def resolve_targets(db, args):
    """Who the demo work should belong to: one named account, or every account present now.

    The studio lists a work only for the account that owns it, so the demo has to be handed to each
    account that exists by the time the tool runs.
    """
    if args.owner and args.owner_uid:
        raise SystemExit('Pass either --owner or --owner-uid, not both.')
    accounts = known_accounts(db)
    if args.owner:
        return [args.owner], accounts
    if args.owner_uid:
        return ['account:' + str(args.owner_uid).strip()], accounts
    return [account['actor'] for account in accounts], accounts


def base_work(bundle):
    """The demo project row inside a bundle, plus the story id it was opened from."""
    work = next((item for item in bundle['records'] if item['kind'] == 'author_project'), None)
    if not work:
        raise SystemExit('This bundle has no author project to hand out.')
    story_id = work['data'].get('zhihu_work_id')
    if not story_id:
        source = next((item for item in bundle['records'] if item['kind'] == 'story_source'), None)
        story_id = (source or {}).get('data', {}).get('work_id')
    return work, str(story_id or work['id'])


def read_manifest(folder):
    path = Path(folder).expanduser().resolve()
    manifest_file = path / 'manifest.json'
    if not manifest_file.is_file():
        raise SystemExit(f'{path} is not a demo bundle: manifest.json is missing.\n'
                         'Produce one with:  python scripts/demo-data.py export --out <folder>')
    return path, json.loads(manifest_file.read_text(encoding='utf-8'))


def assign(bundle, actors, accounts, creator_name=None, creator_avatar=None):
    """Give each target account its own project row over the same finished film."""
    from backend.authors import work_id_for
    from backend.db import Record, Session, uid

    work, story_id = base_work(bundle)
    template = work['data']
    by_actor = {account['actor']: account for account in accounts}
    created, updated = [], []

    with Session.begin() as db:
        for actor in actors:
            pid = work_id_for(story_id, actor)
            data = {**template, 'owner': actor}
            row = db.get(Record, pid)
            if row:
                # Re-running after a new login must not disturb a demo already in place.
                row.data = {**row.data, 'owner': actor}
                updated.append(pid)
                continue
            db.add(Record(id=pid, kind='author_project', data=data))
            created.append(pid)
            db.add(Record(id=uid('audit'), kind='audit', data={
                'target': pid, 'action': 'demo_work_assigned', 'owner': actor,
                'director_id': template.get('director_id'), 'source': 'demo bundle'}))

        # One release serves the whole demo, so its signature is whichever name the operator chose,
        # otherwise the profile of the single account in play. A shared film cannot carry several.
        profile = by_actor.get(actors[0]) if len(actors) == 1 else None
        name = (creator_name or (profile or {}).get('name') or '').strip()
        avatar = creator_avatar or (profile or {}).get('avatar')
        if name or creator_avatar:
            for item in bundle['records']:
                if item['kind'] != 'reader_release':
                    continue
                release = db.get(Record, item['id'])
                if not release:
                    continue
                prior = release.data.get('creator') or {}
                creator = {'name': name or prior.get('name') or '创作者',
                           'avatar_path': avatar if isinstance(avatar, str)
                           and avatar.startswith('https://') else prior.get('avatar_path')}
                if release.data.get('creator') != creator:
                    release.data = {**release.data, 'creator': creator}
                    release.version += 1
    return created, updated


def export(args):
    from backend.db import DATA, Record, Session, Task

    with Session() as db:
        records = list(db.query(Record).all())
        tasks = list(db.query(Task).all())

    director = args.director
    if not director:
        rows = [row for row in records if row.kind == 'director']
        if len(rows) != 1:
            raise SystemExit(f'Expected exactly one director project, found {len(rows)}. '
                             'Pass --director to choose one.')
        director = rows[0].id

    # Every id the chosen work hangs off. Tasks carry these in their payload.
    scopes = {director}
    for row in records:
        if row.kind == 'preproduction' and row.data.get('director_id') == director:
            scopes.add(row.id)
        if row.kind == 'creative_run' and row.id == 'creative_' + director:
            scopes.add(director)

    def belongs(task):
        payload = task.payload or {}
        return any(payload.get(key) in scopes for key in
                   ('creative_id', 'director_id', 'preproduction_id', 'project_id'))

    work = next((row for row in records
                 if row.kind == 'author_project' and row.data.get('director_id') == director), None)
    demo_tasks = [task for task in tasks
                  if task.status in STATUSES and task.kind in TASK_KINDS and (belongs(task)
                      or (work and (task.payload or {}).get('work_id') == work.id))]
    kept_task_ids = {task.id for task in demo_tasks}

    demo_records = [row for row in records if row.kind in RECORD_KINDS]
    if not work:
        raise SystemExit('No author project found for that director project.')

    # The studio reads the node state from the work row. Point it at the coordinators that survived
    # this cut, so an imported demo never shows a step whose task is missing or stopped.
    nodes = {}
    for task in demo_tasks:
        phase = (task.payload or {}).get('production_phase') or (task.payload or {}).get('phase')
        if task.kind in ('author_storyboard', 'author_render') and phase:
            nodes[phase] = task.id

    bundle = []
    for row in demo_records:
        data = row.data
        if row.kind == 'author_project':
            if row.id != work.id:
                continue
            data = {key: value for key, value in data.items()
                    if key not in ('attempt_history', 'source_warning')}
            data = {**data, 'stage': 'published', 'production_nodes': nodes,
                    'supervisor': nodes.get('rendering') or nodes.get('storyboarding')}
            if getattr(args, 'owner', None) is not None:
                data['owner'] = args.owner
        if row.kind == 'reader_release':
            creator = data.get('creator') or {}
            if getattr(args, 'creator_name', None) or getattr(args, 'creator_avatar', None):
                creator = {'name': getattr(args, 'creator_name', None) or creator.get('name') or '创作者',
                           'avatar_path': getattr(args, 'creator_avatar', None) or creator.get('avatar_path')}
            if creator:
                data = {**data, 'creator': creator}
        bundle.append({'id': row.id, 'kind': row.kind, 'version': row.version,
                       'created': row.created, 'data': data})

    # The studio's activity text and skill traces are separate rows keyed by task id.
    for row in records:
        if row.kind not in ('task_activity', 'node_skill_trace'):
            continue
        task_id = row.id.split('_', 1)[1] if row.kind == 'task_activity' else None
        if task_id is None:
            match = re.match(r'skill_trace_(.+)', row.id)
            task_id = match.group(1) if match else ''
        if task_id in kept_task_ids or row.id.split('activity_')[-1] in kept_task_ids:
            bundle.append({'id': row.id, 'kind': row.kind, 'version': row.version,
                           'created': row.created, 'data': row.data})

    out = Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f'{out} is not empty. Pass --force to write into it anyway.')
    (out / 'media').mkdir(parents=True, exist_ok=True)

    # Copy exactly the media the exported records and tasks point at.
    referenced = set()
    for item in bundle:
        referenced.update(MEDIA.findall(json.dumps(item['data'], ensure_ascii=False)))
    manifest_tasks = []
    for task in demo_tasks:
        referenced.update(MEDIA.findall(json.dumps(
            {'payload': task.payload, 'result': task.result}, ensure_ascii=False)))
        manifest_tasks.append({'id': task.id, 'kind': task.kind, 'status': task.status,
                               'session_id': task.session_id, 'revision': task.revision,
                               'payload': task.payload, 'result': task.result,
                               'message': task.message, 'progress': task.progress,
                               'attempts': task.attempts, 'created': task.created})

    copied, missing = 0, []
    for url in sorted(referenced):
        source = media_file(url)
        if not source.is_file():
            missing.append(url)
            continue
        target = out / 'media' / url.removeprefix('/media/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += target.stat().st_size
    if missing:
        raise SystemExit('Media referenced by these records is missing: ' + ', '.join(missing[:5]))

    manifest = {'schema_version': 1, 'built_at': time.time(), 'director_id': director,
                'work_id': work.id, 'records': bundle, 'tasks': manifest_tasks,
                'media': sorted(url.removeprefix('/media/') for url in referenced)}
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                       encoding='utf-8')
    print(f'exported {len(bundle)} records, {len(manifest_tasks)} tasks, '
          f'{len(referenced)} media files ({copied / 1024 / 1024:.1f} MB) to {rel(out)}')
    print('episodes and clips:', ', '.join(sorted(
        {entry.get('shot_id', '')[:3] for entry in
         next((row.data.get('entries') for row in records
               if row.kind == 'reader_release'), []) or []})) or '(no release)')


def import_bundle(args):
    from backend.db import DATA, Record, Session, Task, init_db

    # A freshly reset deployment has an empty database. Creating the tables here means the bundle can
    # be loaded before the services take their first request, instead of requiring a start first.
    init_db()

    folder, manifest = read_manifest(args.folder)

    copied = 0
    for name in manifest['media']:
        source = folder / 'media' / name
        if not source.is_file():
            raise SystemExit(f'The bundle is incomplete: {name} is missing.')
        target = (DATA / 'media' / name).resolve()
        if not target.is_relative_to((DATA / 'media').resolve()):
            raise SystemExit(f'Refusing to write outside the media folder: {name}')
        if target.is_file() and target.stat().st_size == source.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1

    added_records, added_tasks, kept = 0, 0, 0
    with Session.begin() as db:
        for item in manifest['records']:
            if db.get(Record, item['id']):
                kept += 1
                continue
            db.add(Record(id=item['id'], kind=item['kind'], version=item.get('version', 1),
                          created=item.get('created', time.time()), data=item['data']))
            added_records += 1
        for item in manifest['tasks']:
            if db.get(Task, item['id']):
                kept += 1
                continue
            db.add(Task(id=item['id'], kind=item['kind'], status=item['status'],
                        session_id='', revision=item.get('revision', 1),
                        payload=item['payload'], result=item['result'],
                        message=item.get('message', ''), progress=item.get('progress', 100),
                        attempts=item.get('attempts', 1), created=item.get('created', time.time())))
            added_tasks += 1

    # Release manifests are derived files: rebuild them so a fresh deployment can serve them without
    # shipping a second copy of every record.
    exported = 0
    from backend.video_storage import export_manifest
    with Session() as db:
        releases = [row for row in db.query(Record).filter(Record.kind == 'reader_release').all()]
    for release in releases:
        export_manifest(release)
        exported += 1

    print(f'imported {added_records} records, {added_tasks} tasks, {copied} media files; '
          f'{kept} already present; rebuilt {exported} release manifests')
    for item in manifest['records']:
        if item['kind'] == 'reader_release':
            entries = item['data'].get('entries') or []
            creator = item['data'].get('creator') or {}
            print(f"release {item['id']}: {len(entries)} clips, "
                  f"units {item['data'].get('published_units')}/{item['data'].get('total_units')}, "
                  f"creator {creator.get('name') or '(none)'}")

    hand_out(args, manifest)


def hand_out(args, manifest):
    """Report who the demo now belongs to, and say so plainly when nobody can see it yet."""
    from backend.db import Session

    with Session() as db:
        actors, accounts = resolve_targets(db, args)
    if not actors:
        print()
        print('No account has signed in to this deployment yet, so the demo belongs to nobody: a')
        print('public deployment hides ownerless work in the studio. Create the account, sign in')
        print('once, then run:  python scripts/demo-data.py assign <bundle>')
        return
    created, updated = assign(manifest, actors, accounts,
                              creator_name=getattr(args, 'creator_name', None),
                              creator_avatar=getattr(args, 'creator_avatar', None))
    print()
    for actor in actors:
        account = next((item for item in accounts if item['actor'] == actor), None)
        label = (account or {}).get('name') or '(no profile stored)'
        print(f'demo film assigned to {actor} — {label}')
    print(f'project rows created: {len(created)}, already present: {len(updated)}')
    if len(actors) > 1 and not getattr(args, 'creator_name', None):
        print('The release has one signature and several accounts could claim it, so it kept the '
              'name it had.\nPass --creator-name (and --creator-avatar) to sign the demo.')
    print('open the studio with that account and it will be in 继续已有制作.')


def assign_only(args):
    folder, manifest = read_manifest(args.folder)
    from backend.db import Record, Session

    work, _ = base_work(manifest)
    with Session() as db:
        present = bool(db.get(Record, work['id']))
    if not present:
        raise SystemExit('Import this bundle before assigning it: '
                         f'python scripts/demo-data.py import {folder}')
    hand_out(args, manifest)


def list_accounts(args):
    from backend.db import Session

    with Session() as db:
        accounts = known_accounts(db)
    if not accounts:
        print('No Zhihu account has signed in to this deployment yet.')
        return
    for account in accounts:
        print(f"{account['actor']:28} {account['name'] or '(no name stored)'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    cut = commands.add_parser('export', help='cut a demo bundle out of this deployment')
    cut.add_argument('--out', required=True, help='folder to write the bundle into')
    cut.add_argument('--director', help='director project id; default is the only one present')
    cut.add_argument('--owner', help='account or payer id that should own the demo work')
    cut.add_argument('--creator-name', help='public creator name shown on the release')
    cut.add_argument('--creator-avatar', help='https avatar url shown next to the creator name')
    cut.add_argument('--force', action='store_true', help='write into a non-empty folder')
    cut.set_defaults(run=export)

    def targets(parser):
        parser.add_argument('--owner', help='exact actor id, e.g. account:1234')
        parser.add_argument('--owner-uid', help='Zhihu account id to hand the demo work to')
        parser.add_argument('--creator-name', help='public creator name on the release')
        parser.add_argument('--creator-avatar', help='https avatar url for that name')

    load = commands.add_parser('import', help='load a bundle into this deployment')
    load.add_argument('folder', help='bundle folder produced by export')
    targets(load)
    load.set_defaults(run=import_bundle)

    give = commands.add_parser('assign', help='hand an imported bundle to the accounts that exist now')
    give.add_argument('folder', help='bundle folder produced by export')
    targets(give)
    give.set_defaults(run=assign_only)

    commands.add_parser('accounts', help='list the Zhihu accounts this deployment has seen'
                       ).set_defaults(run=list_accounts)

    args = parser.parse_args()
    args.run(args)


if __name__ == '__main__':
    main()
