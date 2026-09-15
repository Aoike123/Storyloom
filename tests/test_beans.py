"""Bean wallet: pricing, gating, refunds and account isolation."""
import pytest

from backend import beans, model_access
from backend.db import Record, Session

UID = '969570047710216200'
OTHER_UID = '9007199254740993'   # one above JS Number.MAX_SAFE_INTEGER


@pytest.fixture
def wallet(monkeypatch):
    monkeypatch.setenv('BEANS_INITIAL_GRANT', '100')
    monkeypatch.setenv('BEANS_LLM_COST', '1')
    monkeypatch.setenv('BEANS_IMAGE_COST', '5')
    monkeypatch.setenv('BEANS_VIDEO_COST_PER_SECOND', '6')
    monkeypatch.setenv('VIDEO_DURATION', '8')
    return monkeypatch


def test_pricing_charges_video_per_requested_second(wallet):
    assert beans.cost_of('llm') == 1
    assert beans.cost_of('image') == 5
    assert beans.cost_of('video') == 48          # 8 seconds x 6
    assert beans.cost_of('video', 5) == 30
    assert beans.cost_of('video', 15) == 90


def test_first_login_grants_once_and_later_logins_do_not_regrant(wallet):
    with Session.begin() as db:
        beans.ensure_wallet(db, UID)
    assert beans.balance(UID) == 100
    with Session.begin() as db:
        beans.ensure_wallet(db, UID)
    assert beans.balance(UID) == 100


def test_debit_and_refund_move_the_balance_and_record_the_task(wallet):
    with Session.begin() as db:
        beans.ensure_wallet(db, UID)
    charged = beans.debit(UID, 'video', 'task-1')
    assert charged['beans_cost'] == '48.00' and charged['beans_after'] == '52.00'
    assert beans.balance(UID) == 52
    assert beans.refund(UID, 'video', 'task-1', charged['beans_cost']) is True
    assert beans.balance(UID) == 100
    ledger = beans.summary(UID)['ledger']
    assert [item['action'] for item in ledger] == ['grant', 'spend', 'refund']
    assert ledger[1]['task'] == 'task-1'


def test_exhausted_wallet_refuses_and_says_what_to_do(wallet):
    with Session.begin() as db:
        beans.ensure_wallet(db, UID)
    beans.debit(UID, 'video', 't1')     # 48 -> 52 left
    beans.debit(UID, 'video', 't2')     # 52 -> 4 left
    assert beans.balance(UID) == 4
    with pytest.raises(beans.BeansExhausted) as raised:
        beans.debit(UID, 'video', 't3')
    assert '算力豆不足' in str(raised.value) and '自己的 API Key' in str(raised.value)
    # A failed debit must not change the balance.
    assert beans.balance(UID) == 4


def test_each_account_has_its_own_wallet(wallet):
    with Session.begin() as db:
        beans.ensure_wallet(db, UID)
        beans.ensure_wallet(db, OTHER_UID)
    beans.debit(UID, 'image', 't1')
    assert beans.balance(UID) == 95
    assert beans.balance(OTHER_UID) == 100
    # Two int64 neighbours must not share one wallet key.
    assert beans.wallet_id('9007199254740992') != beans.wallet_id(OTHER_UID)


def test_account_session_spends_beans_instead_of_the_shared_pool(wallet, monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    monkeypatch.setenv('PUBLIC_POOL_ENABLED', 'true')
    monkeypatch.setattr(model_access, 'public_pool_status',
                        lambda *a, **k: {'available': True, 'selectable': True, 'reason': '可用', 'providers': []})
    created = model_access.create_account_session('session-abc', UID)
    with model_access.access_scope(created['access_id']):
        access = model_access.authorize_call('image', task_id='task-9')
        assert access['mode'] == 'account' and access['uid'] == UID
        assert access['beans_cost'] == '5.00'
        assert model_access.access_paid_states()['image'] is True
    assert beans.balance(UID) == 95
    with Session() as db:
        # Nothing was taken from the anonymous daily pool.
        assert not [row for row in db.query(Record).all() if row.kind == model_access.POOL_KIND]


def test_account_without_beans_reports_every_kind_as_unaffordable(wallet, monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    created = model_access.create_account_session('session-xyz', UID)
    with Session.begin() as db:
        row = beans.ensure_wallet(db, UID)
        row.data = {**row.data, 'beans': '0.00'}
    with model_access.access_scope(created['access_id']):
        assert model_access.access_paid_states() == {'all': False, 'llm': False, 'image': False, 'video': False}
        with pytest.raises(model_access.ModelAccessError, match='算力豆不足'):
            model_access.authorize_call('llm', task_id='t')


def test_refused_provider_call_returns_the_beans(wallet, monkeypatch):
    """A rejected request must not cost the account, matching the shared pool's behaviour."""
    from backend import provider_usage
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    created = model_access.create_account_session('session-refund', UID)
    with model_access.access_scope(created['access_id']):
        access = model_access.authorize_call('image', task_id='task-refund')
        assert beans.balance(UID) == 95
        entry = provider_usage.begin('image', 'test-model', 'task-refund', access)
        provider_usage.finish(entry, status='rejected')
        assert beans.balance(UID) == 100
        assert beans.summary(UID)['ledger'][-1]['action'] == 'refund'


def test_completed_call_keeps_the_charge(wallet):
    from backend import provider_usage
    created = model_access.create_account_session('session-keep', UID)
    with model_access.access_scope(created['access_id']):
        access = model_access.authorize_call('llm', task_id='task-keep')
        entry = provider_usage.begin('llm', 'test-model', 'task-keep', access)
        provider_usage.finish(entry, {'tokens': 10})
    assert beans.balance(UID) == 99


def test_settings_surface_the_balance_for_the_interface(wallet, monkeypatch):
    monkeypatch.setenv('STORYLOOM_DEMO_MODE', 'public')
    created = model_access.create_account_session('session-ui', UID)
    with model_access.access_scope(created['access_id']):
        from backend.providers import settings
        cfg = settings()
        assert cfg['paid_enabled'] is True
        assert cfg['account_beans'] == '100.00'
