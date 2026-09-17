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
    beans_refund = None
    with Session.begin() as db:
        row = db.get(Record, entry)
        if row:
            data = {**row.data, "usage": usage or {}, "status": status}
            if status == "rejected" and not row.data.get("released_at"):
                # The provider refused the request, so this reservation never became a charge.
                data["released_at"] = time.time()
                if data.get("mode") == "account":
                    beans_refund = (data.get("uid"), data.get("kind"), data.get("task"),
                                    data.get("beans_cost"))
            row.data = data
    if beans_refund:
        uid, kind, task, amount = beans_refund
        if uid and kind and amount:
            from . import beans

            beans.refund(uid, kind, task, amount)


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
