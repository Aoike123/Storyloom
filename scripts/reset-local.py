"""Reset the local workbench to a clean slate.

Wipes every record and task in the database and — by default — all generated media, so the next
run starts from nothing. A timestamped database backup is written before anything is deleted.
The worker recreates its heartbeat record on the next start, so it is safe to remove.

    .venv/bin/python scripts/reset-local.py                     # dry run: show what would be cleared
    .venv/bin/python scripts/reset-local.py --yes               # wipe the database and generated media
    .venv/bin/python scripts/reset-local.py --yes --keep-media  # keep the generated media files
"""

import argparse
import os
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()


def database_path() -> Path:
    configured = os.getenv("DATABASE_URL", "")
    if configured.startswith("sqlite:///"):
        return Path(configured.removeprefix("sqlite:///"))
    return DATA / "story.db"


def counts(connection) -> dict:
    return dict(connection.execute("select kind, count(*) from records group by kind"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset the local Storyloom workbench to a clean slate."
    )
    parser.add_argument(
        "--yes", action="store_true", help="actually delete instead of reporting"
    )
    parser.add_argument(
        "--keep-media", action="store_true", help="keep the generated media files"
    )
    args = parser.parse_args()

    db = database_path()
    if not db.is_file():
        print(f"no database at {db} — nothing to reset.")
        return 0

    connection = sqlite3.connect(str(db))
    before = counts(connection)
    records = sum(before.values())
    tasks = connection.execute("select count(*) from tasks").fetchone()[0]
    media = DATA / "media"
    media_files = [p for p in media.rglob("*") if p.is_file()] if media.is_dir() else []
    print(f"database: {db}")
    print(f"  records: {records}   tasks: {tasks}")
    if before:
        print(
            "  records by kind: "
            + ", ".join(f"{k}={v}" for k, v in sorted(before.items()))
        )
    print(f"  media files under {media}: {len(media_files)}")

    if not records and not tasks and not media_files:
        print("\nnothing to reset: this workbench is already a clean starting point.")
        connection.close()
        return 0

    if not args.yes:
        print(
            "\ndry run — add --yes to wipe the database"
            + (" and generated media" if not args.keep_media else "")
            + "."
        )
        connection.close()
        return 0

    backup_dir = DATA / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f'story-{time.strftime("%Y%m%d-%H%M%S")}.db'
    connection.close()
    shutil.copy2(db, backup)
    print(f"\nbackup: {backup}")

    connection = sqlite3.connect(str(db))
    with connection:
        connection.execute("delete from tasks")
        connection.execute("delete from records")
    connection.execute("vacuum")
    connection.close()

    if not args.keep_media and media.is_dir():
        removed = 0
        for item in media.rglob("*"):
            if item.is_file():
                item.unlink()
                removed += 1
        for item in sorted((p for p in media.rglob("*") if p.is_dir()), reverse=True):
            try:
                item.rmdir()
            except OSError:
                pass
        print(f"media files removed: {removed}")

    print("\nworkbench reset. Import a story in the market to start again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
