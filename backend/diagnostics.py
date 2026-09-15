"""Failure reports that keep diagnosis possible without exposing internals to visitors.

A failed task shows a short code to whoever is watching. The full traceback stays on the server
and is only served in local mode, so a public demo cannot leak paths, provider bodies or stack
internals while the operator can still find the real cause.
"""

import re
import secrets
import time
import traceback

from .db import Record, Session

REPORT_KIND = 'failure_report'
_SECRET_PATTERNS = (
    re.compile(r'(?i)bearer\s+[A-Za-z0-9._\-]{8,}'),
    re.compile(r'(?i)(api[_-]?key["\']?\s*[:=]\s*["\']?)[A-Za-z0-9._\-]{8,}'),
    re.compile(r'sk-[A-Za-z0-9]{8,}'),
)


def scrub(text: str) -> str:
    value = str(text or '')
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_redact, value)
    return value


def _redact(match) -> str:
    head = match.group(1) if match.groups() else match.group(0)[:8]
    return f'{head}…redacted'


def new_code() -> str:
    return 'E-' + secrets.token_hex(3).upper()


def record_failure(task_id: str, exc: BaseException, *, stage: str = '', context: dict | None = None) -> str:
    """Persist one failure and return the short code shown to the operator."""
    code = new_code()
    detail = {
        'code': code,
        'task_id': task_id,
        'stage': stage,
        'exception': type(exc).__name__,
        'message': scrub(str(exc))[:1200],
        'traceback': scrub(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[:12000],
        'context': {key: scrub(value)[:400] for key, value in (context or {}).items()},
        'at': time.time(),
    }
    with Session.begin() as db:
        db.add(Record(id='failure_' + code, kind=REPORT_KIND, data=detail))
    return code


def failure_report(code: str) -> dict | None:
    if not isinstance(code, str) or not re.fullmatch(r'E-[0-9A-F]{6}', code.strip().upper()):
        return None
    with Session() as db:
        row = db.get(Record, 'failure_' + code.strip().upper())
        if not row or row.kind != REPORT_KIND:
            return None
        return dict(row.data)
