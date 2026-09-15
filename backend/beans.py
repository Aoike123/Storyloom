"""Prepaid compute beans for signed-in Zhihu accounts.

A signed-in account does not draw on the anonymous shared pool. It spends from its own bean wallet,
granted once at first login. Beans measure the operator's model budget, not money, and are debited
when a call is actually submitted rather than when a task is queued, so a cancelled or
provider-refused call does not cost the account anything. When the wallet runs out the account must
switch to its own API keys, which is the behaviour the interface explains at that point.
"""

import hashlib
import os
import time
from decimal import Decimal, InvalidOperation
from threading import Lock

from sqlalchemy import text

from .db import Record, Session, bounded

WALLET_KIND = 'bean_wallet'
LEDGER_LIMIT = 60
_lock = Lock()

_COSTS = {
    'llm': ('BEANS_LLM_COST', '1'),
    'image': ('BEANS_IMAGE_COST', '5'),
}
_VIDEO_PER_SECOND = ('BEANS_VIDEO_COST_PER_SECOND', '6')
_INITIAL_GRANT = ('BEANS_INITIAL_GRANT', '500')
_DEFAULT_VIDEO_SECONDS = 8


class BeansExhausted(Exception):
    """The account cannot pay for this call from its bean wallet."""


def _amount(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default).strip())
    except (InvalidOperation, AttributeError):
        value = Decimal(default)
    if value < 0 or value > Decimal('1000000'):
        value = Decimal(default)
    return value.quantize(Decimal('0.01'))


def _video_seconds(duration_seconds=None) -> int:
    try:
        seconds = int(duration_seconds) if duration_seconds is not None else int(
            os.getenv('VIDEO_DURATION', str(_DEFAULT_VIDEO_SECONDS)))
    except (TypeError, ValueError):
        seconds = _DEFAULT_VIDEO_SECONDS
    return max(1, min(seconds, 60))


def cost_of(kind: str, duration_seconds=None) -> Decimal:
    """Beans one call of this kind costs. Video is charged per requested second."""
    if kind == 'video':
        return (_amount(*_VIDEO_PER_SECOND) * _video_seconds(duration_seconds)).quantize(Decimal('0.01'))
    return _amount(*_COSTS[kind]) if kind in _COSTS else Decimal('0.00')


def initial_grant() -> Decimal:
    return _amount(*_INITIAL_GRANT)


def wallet_id(uid: str) -> str:
    """Wallet key. ``uid`` is stored as text end to end, so two int64 neighbours stay distinct."""
    return 'bean_wallet_' + hashlib.sha256(str(uid).encode('utf-8')).hexdigest()[:32]


def _lock_wallet(db) -> None:
    """Serialize balance changes across the API and worker processes."""
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(782347190323)'))


def _balance(row) -> Decimal:
    try:
        return Decimal(str(row.data.get('beans', '0')))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


def _entry(action, kind, amount, task_id, balance, detail=None):
    return {'at': time.time(), 'action': action, 'kind': kind, 'amount': f'{amount:.2f}',
            'balance': f'{balance:.2f}', 'task': task_id, **({'detail': detail} if detail else {})}


def ensure_wallet(db, uid: str) -> Record:
    """Create the wallet with its one-time grant, or return the existing one."""
    key = wallet_id(uid)
    row = db.get(Record, key)
    if row:
        return row
    grant = initial_grant()
    row = Record(id=key, kind=WALLET_KIND, data={
        'uid': str(uid), 'beans': f'{grant:.2f}', 'granted': f'{grant:.2f}',
        'created_at': time.time(), 'updated_at': time.time(),
        'ledger': [_entry('grant', 'account', grant, None, grant, '首次登录赠送')],
    })
    db.add(row)
    db.flush()
    return row


def balance(uid: str) -> Decimal:
    """Current balance without creating a wallet for an account that never logged in."""
    with Session() as db:
        row = db.get(Record, wallet_id(uid))
        return _balance(row) if row and row.kind == WALLET_KIND else Decimal('0')


def affordable(uid: str, kind: str, duration_seconds=None) -> bool:
    return balance(uid) >= cost_of(kind, duration_seconds)


def debit(uid: str, kind: str, task_id: str | None, duration_seconds=None) -> dict:
    """Charge one call. Raises BeansExhausted when the wallet cannot cover it."""
    amount = cost_of(kind, duration_seconds)
    if amount <= 0:
        return {'beans_cost': '0.00', 'beans_after': f'{balance(uid):.2f}'}
    with _lock, Session.begin() as db:
        _lock_wallet(db)
        row = ensure_wallet(db, uid)
        current = _balance(row)
        if current < amount:
            raise BeansExhausted(
                f'算力豆不足：这一步需要 {amount:.0f} 豆，账户还剩 {current:.0f} 豆。'
                '请填写自己的 API Key 继续，或等待新的赠送。')
        after = current - amount
        data = dict(row.data)
        data['beans'] = f'{after:.2f}'
        data['updated_at'] = time.time()
        data['ledger'] = bounded(data.get('ledger'), _entry('spend', kind, amount, task_id, after),
                                 LEDGER_LIMIT)
        row.data = data
    return {'beans_cost': f'{amount:.2f}', 'beans_after': f'{after:.2f}'}


def refund(uid: str, kind: str, task_id: str | None, amount, detail='供应商未受理，已退回') -> bool:
    """Return a charge that never became a real call. Repeating this is harmless only once per
    ledger entry, which the caller guards with the usage record."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if value <= 0:
        return False
    with _lock, Session.begin() as db:
        _lock_wallet(db)
        row = db.get(Record, wallet_id(uid))
        if not row or row.kind != WALLET_KIND:
            return False
        after = _balance(row) + value
        data = dict(row.data)
        data['beans'] = f'{after:.2f}'
        data['updated_at'] = time.time()
        data['ledger'] = bounded(data.get('ledger'), _entry('refund', kind, value, task_id, after, detail),
                                 LEDGER_LIMIT)
        row.data = data
    return True


def summary(uid: str) -> dict:
    """Wallet view for the account UI: balance, grant and the newest ledger entries."""
    with Session() as db:
        row = db.get(Record, wallet_id(uid))
        if not row or row.kind != WALLET_KIND:
            return {'uid': str(uid), 'beans': '0.00', 'granted': '0.00', 'ledger': [],
                    'costs': {'llm': f'{cost_of("llm"):.0f}', 'image': f'{cost_of("image"):.0f}',
                              'video': f'{cost_of("video"):.0f}'}}
        data = dict(row.data)
    return {'uid': str(uid), 'beans': data.get('beans', '0.00'), 'granted': data.get('granted', '0.00'),
            'ledger': list(data.get('ledger', []))[-20:],
            'costs': {'llm': f'{cost_of("llm"):.0f}', 'image': f'{cost_of("image"):.0f}',
                      'video': f'{cost_of("video"):.0f}'}}
