"""Zhihu account login for Storyloom.

Identity only. The OAuth access token stays on the server, encrypted at rest, and is never sent to
a browser, a log line or a URL. The account id comes from Zhihu's ``/user`` endpoint and is kept as
text: ``uid`` is an int64 that JavaScript cannot represent exactly, so treating it as a number
would let two different people share one wallet.

Protocol notes verified from the official zhihu skill (0.7.2-beta):

- authorize: ``https://openapi.zhihu.com/authorize`` with ``redirect_uri``, ``app_id``,
  ``response_type=code`` and ``state``
- the callback carries ``authorization_code`` (older docs said ``code``); accept both
- token exchange posts form fields where the field itself is named ``code``
- the hackathon OAuth service returns ``state`` unchanged, so a mismatch is rejected
- business success may still arrive as HTTP 200, so check for the token or the user id itself
"""

import hashlib
import os
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .db import Record, Session
from .model_access import ModelAccessError, open_secret, public_demo_mode, seal_secret

router = APIRouter(prefix='/api/zhihu', tags=['zhihu-login'])

AUTHORIZE_URL = 'https://openapi.zhihu.com/authorize'
TOKEN_URL = 'https://openapi.zhihu.com/access_token'
PROFILE_URL = 'https://openapi.zhihu.com/user'
COOKIE_NAME = 'storyloom_zhihu_session'
SESSION_KIND = 'zhihu_login'
STATE_KIND = 'zhihu_oauth_state'
SESSION_PATTERN = re.compile(r'^[A-Za-z0-9_-]{16,80}$')
STATE_PATTERN = re.compile(r'^[A-Za-z0-9_-]{16,80}$')
STATE_TTL = 600
LOGIN_TTL = 8 * 3600


def _config():
    """Deployment settings. The app key is read straight from the environment and is never part
    of the editable model configuration, so it cannot leak through the settings endpoint."""
    return {
        'app_id': (os.getenv('ZHIHU_OAUTH_APP_ID') or '').strip(),
        'app_key': (os.getenv('ZHIHU_OAUTH_APP_KEY') or '').strip(),
        'redirect_uri': (os.getenv('ZHIHU_OAUTH_REDIRECT_URI') or '').strip(),
    }


def configured() -> bool:
    cfg = _config()
    return bool(cfg['app_id'] and cfg['app_key'] and cfg['redirect_uri'])


def _session_id(request: Request) -> str | None:
    value = request.cookies.get(COOKIE_NAME)
    return value if value and SESSION_PATTERN.fullmatch(value) else None


def _ensure_session(request: Request, response: Response) -> str:
    existing = _session_id(request)
    if existing:
        return existing
    created = secrets.token_urlsafe(32)
    response.set_cookie(
        COOKIE_NAME, created, max_age=LOGIN_TTL, httponly=True, samesite='lax',
        secure=public_demo_mode(), path='/',
    )
    return created


def _session_record_id(session_id: str) -> str:
    return 'zhihu_login_' + hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _state_record_id(state: str) -> str:
    return 'zhihu_state_' + hashlib.sha256(state.encode()).hexdigest()[:32]


def current_account(request: Request) -> dict | None:
    """The signed-in Zhihu account for this browser, or None."""
    session_id = _session_id(request)
    if not session_id:
        return None
    with Session() as db:
        row = db.get(Record, _session_record_id(session_id))
        if not row or row.kind != SESSION_KIND:
            return None
        data = dict(row.data)
    if float(data.get('expires_at', 0)) <= time.time():
        return None
    return data


def _authorize_url(state: str) -> str:
    cfg = _config()
    return AUTHORIZE_URL + '?' + urlencode({
        'redirect_uri': cfg['redirect_uri'],
        'app_id': cfg['app_id'],
        'response_type': 'code',
        'state': state,
    })


def _post_form(url: str, form: dict) -> dict:
    try:
        response = httpx.post(
            url, content=urlencode(form), headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=20, follow_redirects=False,
        )
    except httpx.HTTPError:
        # The token exchange is not retried: a retry could consume the one-time code twice.
        raise HTTPException(502, '连接知乎令牌接口失败，未自动重试，请重新登录。') from None
    if response.status_code >= 400:
        raise HTTPException(502, f'知乎令牌接口返回 HTTP {response.status_code}，未自动重试。')
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(502, '知乎令牌接口未返回有效 JSON。') from None
    if not isinstance(payload, dict):
        raise HTTPException(502, '知乎令牌接口返回格式不符合约定。')
    return payload


def _get_profile(token: str) -> dict:
    try:
        response = httpx.get(
            PROFILE_URL, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
            timeout=20, follow_redirects=False,
        )
    except httpx.HTTPError:
        raise HTTPException(502, '连接知乎用户信息接口失败。') from None
    if response.status_code >= 400:
        raise HTTPException(502, f'知乎用户信息接口返回 HTTP {response.status_code}。')
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(502, '知乎用户信息接口未返回有效 JSON。') from None
    if not isinstance(payload, dict):
        raise HTTPException(502, '知乎用户信息接口返回格式不符合约定。')
    source = payload.get('data') or payload.get('Data')
    return source if isinstance(source, dict) else payload


def _account_from(source: dict) -> dict:
    """Normalize the profile. ``uid`` is required, because it is the wallet key."""
    raw_uid = source.get('uid') or source.get('Uid') or source.get('id')
    if raw_uid is None or isinstance(raw_uid, bool):
        raise HTTPException(502, '知乎用户信息接口没有返回用户标识，不能建立登录会话。')
    uid = str(raw_uid).strip()
    if not uid.isdigit():
        raise HTTPException(502, '知乎用户标识格式不符合约定。')
    return {
        'uid': uid,
        'hash_id': source.get('hash_id') or source.get('HashId'),
        'fullname': source.get('fullname') or source.get('Fullname') or source.get('name'),
        'avatar_path': source.get('avatar_path') or source.get('AvatarPath') or source.get('avatar_url'),
        'headline': source.get('headline') or source.get('Headline'),
        'url': source.get('url') or source.get('Url'),
    }


def _token_from(payload: dict) -> tuple[str, int | None]:
    source = payload.get('data') or payload.get('Data')
    fields = source if isinstance(source, dict) else payload
    token = fields.get('access_token') or fields.get('AccessToken')
    if not isinstance(token, str) or not token.strip():
        raise HTTPException(502, '未获得知乎 OAuth access token，未自动重试。')
    try:
        expires = int(fields.get('expires_in') or 0)
    except (TypeError, ValueError):
        expires = 0
    return token.strip(), (expires if expires > 0 else None)


@router.get('/status')
def status(request: Request):
    account = current_account(request)
    return {
        'configured': configured(),
        'authorized': account is not None,
        'account': public_account(account) if account else None,
    }


def public_account(account: dict) -> dict:
    """The account shape the browser may see. Never includes the token."""
    return {
        'uid': account.get('uid'),
        'fullname': account.get('fullname'),
        'avatar_path': account.get('avatar_path'),
        'headline': account.get('headline'),
        'url': account.get('url'),
        'expires_at': account.get('expires_at'),
    }


@router.get('/login')
def login(request: Request):
    if not configured():
        raise HTTPException(409, '知乎登录尚未配置：缺少 App ID、App Key 或公网回调地址。')
    response = RedirectResponse('/', status_code=302)
    session_id = _ensure_session(request, response)
    state = secrets.token_urlsafe(32)
    now = time.time()
    with Session.begin() as db:
        db.add(Record(id=_state_record_id(state), kind=STATE_KIND, data={
            'session_id': session_id, 'created_at': now, 'expires_at': now + STATE_TTL,
        }))
    response.headers['Location'] = _authorize_url(state)
    return response


@router.get('/callback')
def callback(request: Request, authorization_code: str = '', code: str = '', state: str = '', error: str = ''):
    if error:
        return RedirectResponse('/?zhihu=denied', status_code=302)
    granted = (authorization_code or code).strip()
    if not granted:
        return RedirectResponse('/?zhihu=error', status_code=302)
    if not STATE_PATTERN.fullmatch(state or ''):
        return RedirectResponse('/?zhihu=error', status_code=302)

    session_id = _session_id(request)
    if not session_id:
        return RedirectResponse('/?zhihu=error', status_code=302)

    # Consume the state exactly once, inside one transaction, and only for the browser that
    # started this login. A missing, reused, expired or mismatched value never reaches the token
    # exchange.
    record_id = _state_record_id(state)
    with Session.begin() as db:
        row = db.get(Record, record_id)
        if not row or row.kind != STATE_KIND:
            return RedirectResponse('/?zhihu=error', status_code=302)
        data = dict(row.data)
        db.delete(row)
        if data.get('session_id') != session_id or float(data.get('expires_at', 0)) <= time.time():
            return RedirectResponse('/?zhihu=error', status_code=302)

    cfg = _config()
    try:
        payload = _post_form(TOKEN_URL, {
            'app_id': cfg['app_id'],
            'app_key': cfg['app_key'],
            'grant_type': 'authorization_code',
            'redirect_uri': cfg['redirect_uri'],
            'code': granted,
        })
        token, expires_in = _token_from(payload)
        account = _account_from(_get_profile(token))
    except HTTPException as exc:
        # A login that cannot be completed returns the visitor to the page with a short reason.
        # The detail stays server-side so the operator can still diagnose it; no credential is
        # recorded.
        with Session.begin() as db:
            db.add(Record(id='zhihu_login_failure_' + secrets.token_hex(8), kind='audit', data={
                'target': 'zhihu_oauth', 'action': 'zhihu_login_failed',
                'reason': str(exc.detail)[:300], 'at': time.time(),
            }))
        return RedirectResponse('/?zhihu=error', status_code=302)
    try:
        sealed = seal_secret(token)
    except ModelAccessError:
        sealed = ''
    now = time.time()
    with Session.begin() as db:
        key = _session_record_id(session_id)
        existing = db.get(Record, key)
        stored = {
            **account, 'token': sealed, 'state_verified': True,
            'authorized_at': now, 'expires_at': now + (expires_in or LOGIN_TTL),
        }
        if existing:
            existing.data = stored
            existing.version += 1
        else:
            db.add(Record(id=key, kind=SESSION_KIND, data=stored))
        db.add(Record(id='zhihu_login_audit_' + secrets.token_hex(8), kind='audit', data={
            'target': 'zhihu_oauth', 'action': 'zhihu_login_succeeded',
            'uid': account['uid'], 'at': now,
        }))
    return RedirectResponse('/?zhihu=ok', status_code=302)


@router.post('/logout')
def logout(request: Request):
    session_id = _session_id(request)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path='/')
    if not session_id:
        return response
    with Session.begin() as db:
        row = db.get(Record, _session_record_id(session_id))
        if row and row.kind == SESSION_KIND:
            # Drop the server-side session and its token together.
            db.delete(row)
    return response


def account_token(request: Request) -> str | None:
    """Decrypted OAuth token, for server-side calls only. Never returned to a browser."""
    account = current_account(request)
    if not account or not account.get('token'):
        return None
    return open_secret(account['token'])
