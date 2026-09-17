"""Clear local works, tasks and generated media so the next run starts from nothing.

Keeps everything about *who pays* — accounts, bean wallets, attached-key sessions — so a reset
does not sign the operator out or reset a wallet. Always writes a database backup first.

    .venv\\Scripts\\python.exe scripts\\reset-works.py            # show what would go
    .venv\\Scripts\\python.exe scripts\\reset-works.py --yes      # clear works and tasks
    .venv\\Scripts\\python.exe scripts\\reset-works.py --yes --media   # also delete generated files
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).resolve()

# Everything that describes a work, its production chain, or a paid call made for it.
WORK_KINDS = (
    'author_project', 'director', 'story_source', 'creative_run',
    'preproduction', 'preproduction_gate', 'visual_config', 'visual_gate',
    'asset', 'asset_revision', 'clip', 'artifact', 'shot_revision',
    'reader_release', 'reader_wish', 'reader_branch', 'reader_session',
    'audit', 'failure_report', 'usage', 'node_skill_trace', 'node_output',
    'style_live',
)
# Who pays, and caches: a reset must not sign the operator out or empty a wallet.
KEEP_KINDS = (
    'system', 'model_access_session', 'bean_wallet', 'operator_key_day',
    'zhihu_login', 'zhihu_oauth_state', 'zhihu_cache', 'public_pool_balance',
)


def database_path() -> Path:
    configured = os.getenv('DATABASE_URL', '')
    if configured.startswith('sqlite:///'):
        return Path(configured.removeprefix('sqlite:///'))
    return DATA / 'story.db'


def counts(connection) -> dict:
    return dict(connection.execute('select kind, count(*) from records group by kind'))


def main() -> int:
    parser = argparse.ArgumentParser(description='Clear local Storyloom works and tasks.')
    parser.add_argument('--yes', action='store_true', help='actually delete instead of reporting')
    parser.add_argument('--media', action='store_true', help='also delete generated media files')
    parser.add_argument('--keep-usage', action='store_true', help='keep the paid-call history')
    args = parser.parse_args()

    db = database_path()
    if not db.is_file():
        print(f'no database at {db}')
        return 0

    connection = sqlite3.connect(str(db))
    before = counts(connection)
    tasks = connection.execute('select count(*) from tasks').fetchone()[0]
    works = before.get('author_project', 0)
    print(f'database: {db}')
    print(f'  works: {works}   tasks: {tasks}')
    print('  records by kind: ' + (', '.join(f'{k}={v}' for k, v in sorted(before.items())) or '(none)'))

    kinds = [k for k in WORK_KINDS if not (args.keep_usage and k == 'usage')]
    targets = {k: before.get(k, 0) for k in kinds if before.get(k)}
    if not targets and not tasks and not args.media:
        print('\nnothing to clear: this database is already a clean starting point.')
        connection.close()
        return 0

    print('\nwill clear:')
    for kind, count in sorted(targets.items()):
        print(f'  {kind}: {count}')
    if tasks:
        print(f'  tasks: {tasks}')
    if args.media:
        print(f'  generated media under {DATA / "media"}')
    if not args.yes:
        print('\ndry run — add --yes to perform this.')
        connection.close()
        return 0

    backup_dir = DATA / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f'story-{time.strftime("%Y%m%d-%H%M%S")}.db'
    connection.close()
    shutil.copy2(db, backup)
    print(f'\nbackup: {backup}')

    connection = sqlite3.connect(str(db))
    with connection:
        connection.execute('delete from tasks')
        for kind in kinds:
            connection.execute('delete from records where kind = ?', (kind,))
    connection.execute('vacuum')
    after = counts(connection)
    connection.close()

    if args.media:
        media = DATA / 'media'
        removed = 0
        for item in media.rglob('*'):
            if item.is_file():
                item.unlink()
                removed += 1
        for item in sorted((p for p in media.rglob('*') if p.is_dir()), reverse=True):
            try:
                item.rmdir()
            except OSError:
                pass
        print(f'media files removed: {removed}')

    print('\nafter:')
    print('  records by kind: ' + (', '.join(f'{k}={v}' for k, v in sorted(after.items())) or '(none)'))
    kept = sorted(set(after) & set(KEEP_KINDS))
    print('  kept: ' + (', '.join(kept) or '(nothing)'))
    print('\nready for a clean run.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
