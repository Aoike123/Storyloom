"""Record actual provider submissions and reported usage for audit/recovery.

Budget reservations are only refunded here when the provider rejected the request, which is the
one case where nothing could have been charged. Uncertain outcomes (timeouts, 5xx) keep the
reservation because the call may still be billed.
"""

import time

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
    refund = None
    with Session.begin() as db:
        row = db.get(Record, entry)
        if row:
            data = {**row.data, "usage": usage or {}, "status": status}
            if status == "rejected" and not row.data.get("released_at"):
                # The provider refused the request, so this reservation never became a charge.
                data["released_at"] = time.time()
                if data.get("mode") == "public":
                    refund = (data.get("pool_kind"), data.get("reserved_cny"), data.get("pool_day"))
            row.data = data
    if refund:
        from .model_access import release_public_call

        kind, amount, day = refund
        if kind and amount:
            release_public_call(kind, amount, day)


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
