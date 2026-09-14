"""Record actual provider submissions and reported usage for audit/recovery."""

from sqlalchemy import select

from .db import Record, Session, uid


def begin(kind, model, task, access=None):
    entry = uid("usage")
    with Session.begin() as db:
        db.add(
            Record(
                id=entry,
                kind="usage",
                data={
                    "task": task,
                    "provider": kind,
                    "model": model,
                    "submissions": 1,
                    "status": "pending",
                    "usage": {},
                    **(access or {}),
                },
            )
        )
    return entry


def finish(entry, usage=None, status="completed"):
    if not entry:
        return
    with Session.begin() as db:
        row = db.get(Record, entry)
        if row:
            row.data = {**row.data, "usage": usage or {}, "status": status}


def finish_video(task, usage):
    with Session() as db:
        rows = db.scalars(
            select(Record).where(Record.kind == "usage").order_by(Record.created.desc())
        ).all()
        entry = next(
            (
                row.id
                for row in rows
                if row.data.get("task") == task and row.data.get("provider") == "video"
            ),
            None,
        )
    finish(entry, usage)
