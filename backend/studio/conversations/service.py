"""Storyloom Studio — 会话域服务层（附录 10 §1–§3 命令的服务端实现）。

CHAT-01 冻结（C2）：会话「取或建」、会话列表、会话详情、消息列表、
消息追加五条命令；消息只追加，会话 updated_at 随追加消息推进。

实现约束（冻结）：
- 所有写命令走 command_id 幂等（附录 00 §3，studio_command_records）；
  同 (owner_id, command_id) 且内容相同 → 重放首次 result_payload；
  同 command_id 异内容 → 422 validation_failed field=command_id rule=reused；
- 属主隔离：未登录 401；跨属主 / 跨项目 / 不存在一律 404 not_found
  （不泄漏存在性）；
- id 全部服务端 ULID（core.db.make_ulid）；
- 错误一律 raise StudioAPIError（冻结错误协议）；
- 本卡只读业务表：作用域存在性只 SELECT Fragment/Scene/Shot/Asset/EditDraft，
  job_id 只 SELECT StudioJob；不写任何业务表、不建提案、不调用任何模型、
  不发任何网络请求；title 固定空串（改名命令属后续卡）。
"""
import json
import re
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..assets.models import Asset
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..edits.models import EditDraft
from ..jobs.models import StudioJob
from ..projects.models import StudioProject
from ..sources.fragment_models import Fragment
from ..storyboard.models import Scene, Shot
from .models import Conversation, Message

__all__ = [
    "get_conversation",
    "list_conversations",
    "list_messages",
    "open_conversation",
    "post_message",
]

# 附录 10 §1–§3 冻结取值
_SCOPE_KINDS = ("project", "fragment", "scene", "shot", "asset", "edit")
_SENDERS = ("user", "assistant")
_MAX_CONTENT = 8000
_DETAIL_MESSAGE_LIMIT = 50

# 附录 00 §6 查询 DTO 裁定：cursor 必须是 26 字符 Crockford base32 小写
_CURSOR_RE = re.compile(r"^[0-9a-z]{26}$")


# ────────────────────────────── 内部工具 ──────────────────────────────


def _require_project(session, user, pid) -> StudioProject:
    """项目必须存在且属当前用户，否则 404 not_found（不泄漏存在性）。"""
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)
    return project


def _require_conversation(session, user, pid, cid) -> Conversation:
    """会话必须存在、属本用户且属本请求项目，否则 404 not_found（同形状）。"""
    conversation = session.scalar(select(Conversation).where(Conversation.id == cid))
    if (
        conversation is None
        or conversation.project_id != pid
        or conversation.owner_id != user.id
    ):
        raise StudioAPIError.not_found("conversation", cid)
    return conversation


def _check_cursor(cursor: str | None) -> None:
    """cursor 格式校验（逐字对齐 P2）：非 26 字符小写 → 422 field=cursor rule=format。"""
    if cursor is not None and not _CURSOR_RE.match(cursor):
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "cursor",
                    "rule": "format",
                    "message": "cursor 必须是 26 字符 ULID。",
                }
            ]
        )


def _clamp_limit(limit) -> int:
    """limit 缺省 50、clamp 到 [1, 200]（越界不报 422）。"""
    return max(1, min(int(limit), 200))


def _scope_exists(session, pid: str, scope_kind: str, scope_id: str) -> bool:
    """作用域 id 是否存在于本项目（只读 SELECT，绝不写业务表）。

    fragment→studio_fragments、scene→studio_scenes、shot→studio_shots、
    asset→studio_assets、edit→studio_edit_drafts.edit_id。
    """
    if scope_kind == "fragment":
        return (
            session.scalar(
                select(Fragment.id).where(
                    Fragment.id == scope_id, Fragment.project_id == pid
                )
            )
            is not None
        )
    if scope_kind == "scene":
        return (
            session.scalar(
                select(Scene.id).where(Scene.id == scope_id, Scene.project_id == pid)
            )
            is not None
        )
    if scope_kind == "shot":
        return (
            session.scalar(
                select(Shot.id).where(Shot.id == scope_id, Shot.project_id == pid)
            )
            is not None
        )
    if scope_kind == "asset":
        return (
            session.scalar(
                select(Asset.id).where(Asset.id == scope_id, Asset.project_id == pid)
            )
            is not None
        )
    if scope_kind == "edit":
        return (
            session.scalar(
                select(EditDraft.id)
                .where(EditDraft.edit_id == scope_id, EditDraft.project_id == pid)
                .limit(1)
            )
            is not None
        )
    return False


def _conversation_dto(conversation: Conversation) -> dict:
    """ConversationDto（逐字节冻结形状）。"""
    return {
        "object_ref": {
            "kind": "conversation",
            "id": conversation.id,
            "revision": None,
        },
        "scope_kind": conversation.scope_kind,
        "scope_id": conversation.scope_id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _message_dto(message: Message) -> dict:
    """MessageDto（逐字节冻结形状）；refs 入库为 JSON 文本、出参解析为数组。"""
    return {
        "object_ref": {"kind": "message", "id": message.id, "revision": None},
        "conversation_id": message.conversation_id,
        "sender": message.sender,
        "content": message.content,
        "refs": json.loads(message.refs) if message.refs else None,
        "job_id": message.job_id,
        "created_at": message.created_at,
    }


def _load_command(session, user, command_id: str) -> CommandRecord | None:
    """按 (owner_id, command_id) 读幂等记录（读查询，先于写事务）。"""
    return session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )


def _payload_of(record: CommandRecord) -> dict:
    return json.loads(record.result_payload) if record.result_payload else {}


def _reused_error() -> StudioAPIError:
    """同 command_id 异内容（冻结码表无专用码，validation_failed 承载）。"""
    return StudioAPIError.validation_failed(
        [
            {
                "field": "command_id",
                "rule": "reused",
                "message": "command_id 已被其他命令使用，请生成新的。",
            }
        ]
    )


def _refs_violation() -> StudioAPIError:
    return StudioAPIError.validation_failed(
        [
            {
                "field": "refs",
                "rule": "invalid",
                "message": "refs 必须是数组，且每项为 {kind, id, label}。",
            }
        ]
    )


def _normalize_refs(refs):
    """校验并规范化 refs：None→None；合法数组→[{kind,id,label}]（只留冻结三键）。

    非 null 时必须是数组，且每项 kind/id 为非空 str、label 为 str，否则 422
    field=refs rule=invalid（按序短路：一次性判定，不收集多条）。
    """
    if refs is None:
        return None
    if not isinstance(refs, list):
        raise _refs_violation()
    normalized: list[dict[str, str]] = []
    for item in refs:
        if not isinstance(item, dict):
            raise _refs_violation()
        kind = item.get("kind")
        ref_id = item.get("id")
        label = item.get("label")
        if not isinstance(kind, str) or not kind:
            raise _refs_violation()
        if not isinstance(ref_id, str) or not ref_id:
            raise _refs_violation()
        if not isinstance(label, str):
            raise _refs_violation()
        normalized.append({"kind": kind, "id": ref_id, "label": label})
    return normalized


def _existing_conversation(session, user, pid, scope_kind, scope_id):
    return session.scalar(
        select(Conversation).where(
            Conversation.project_id == pid,
            Conversation.scope_kind == scope_kind,
            Conversation.scope_id == scope_id,
            Conversation.owner_id == user.id,
        )
    )


# ────────────────────────────── 1. 会话列表 ──────────────────────────────


def list_conversations(
    session, user, pid: str, scope_kind, scope_id, cursor, limit
) -> dict:
    """GET /projects/{pid}/conversations：200 {items, next_cursor}。

    纯读：不写任何表、不 commit、不接受 command_id。只返回
    owner_id==user.id 且 project_id==pid 的行（他人会话不可见）；
    scope_kind/scope_id 为可选过滤项；按 id 升序（keyset：cursor 后取 id>cursor）。
    """
    _require_project(session, user, pid)
    _check_cursor(cursor)
    limit = _clamp_limit(limit)

    stmt = select(Conversation).where(
        Conversation.project_id == pid, Conversation.owner_id == user.id
    )
    if scope_kind is not None:
        stmt = stmt.where(Conversation.scope_kind == scope_kind)
    if scope_id is not None:
        stmt = stmt.where(Conversation.scope_id == scope_id)
    if cursor is not None:
        stmt = stmt.where(Conversation.id > cursor)

    rows = session.scalars(
        stmt.order_by(Conversation.id.asc()).limit(limit + 1)
    ).all()
    items = [_conversation_dto(c) for c in rows[:limit]]
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


# ─────────────────────── 2. 会话「取或建」（POST） ───────────────────────


def open_conversation(session, user, pid: str, scope_kind, scope_id, command_id) -> dict:
    """POST /projects/{pid}/conversations：取或建。

    校验序（按序短路）：项目 404 → scope_kind 枚举 422 → scope_id 归属
    （project 必须 ==pid，否则 422 mismatch；其余 kind 必须存在且属本项目，
    否则 404 {kind:scope_kind, id:scope_id}）→ 幂等（同 command_id 同内容
    → 200 原 result_payload；异内容 → 422 reused）→ 已存在四元组行 → 200
    该行 DTO（取或建，不报 duplicate）→ 201 新建。

    返回 DTO；命中重放或已存在时附带内部标记 ``_replay=True``（路由层 pop
    后以 200 返回，不改变响应体形状）。新建走单事务 INSERT Conversation +
    INSERT CommandRecord；title 固定空串。
    """
    _require_project(session, user, pid)

    if scope_kind not in _SCOPE_KINDS:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "scope_kind",
                    "rule": "enum",
                    "message": "scope_kind 必须是 project/fragment/scene/shot/asset/edit 之一。",
                }
            ]
        )
    if scope_kind == "project":
        if scope_id != pid:
            raise StudioAPIError.validation_failed(
                [
                    {
                        "field": "scope_id",
                        "rule": "mismatch",
                        "message": "scope_kind=project 时 scope_id 必须等于项目 id。",
                    }
                ]
            )
    elif not _scope_exists(session, pid, scope_kind, scope_id):
        raise StudioAPIError.not_found(scope_kind, scope_id)

    record = _load_command(session, user, command_id)
    if record is not None:
        payload = _payload_of(record)
        same = (
            payload.get("scope_kind") == scope_kind
            and payload.get("scope_id") == scope_id
            and (record.project_id is None or record.project_id == pid)
        )
        if not same:
            raise _reused_error()
        return dict(payload, _replay=True)

    existing = _existing_conversation(session, user, pid, scope_kind, scope_id)
    if existing is not None:
        return dict(_conversation_dto(existing), _replay=True)

    now = time.time()
    conversation = Conversation(
        id=make_ulid(),
        project_id=pid,
        owner_id=user.id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        title="",
        created_at=now,
        updated_at=now,
    )
    dto = _conversation_dto(conversation)
    command = CommandRecord(
        id=make_ulid(),
        owner_id=user.id,
        project_id=pid,
        command_id=command_id,
        result_payload=json.dumps(dto, ensure_ascii=False),
        created_at=now,
    )
    session.commit()  # 结束只读事务；写阶段另起事务
    try:
        with session.begin():
            session.add(conversation)
            session.flush()  # 先持久化父行：UOW 不保证跨表 FK 插入序
            session.add(command)
    except IntegrityError:
        # 并发竞态兜底（UQ 四元组 / UQ owner+command）：回滚后按同一语义重判
        session.rollback()
        return _recover_open(session, user, pid, scope_kind, scope_id, command_id)
    return dto


def _recover_open(session, user, pid, scope_kind, scope_id, command_id) -> dict:
    """新建事务因唯一约束失败后的确定性兜底（不产生第二次写）。"""
    record = _load_command(session, user, command_id)
    if record is not None:
        payload = _payload_of(record)
        same = (
            payload.get("scope_kind") == scope_kind
            and payload.get("scope_id") == scope_id
            and (record.project_id is None or record.project_id == pid)
        )
        if not same:
            raise _reused_error()
        return dict(payload, _replay=True)
    existing = _existing_conversation(session, user, pid, scope_kind, scope_id)
    if existing is not None:
        return dict(_conversation_dto(existing), _replay=True)
    raise StudioAPIError.internal()


# ────────────────────────── 3. 会话详情（GET） ──────────────────────────


def get_conversation(session, user, pid: str, cid: str) -> dict:
    """GET /projects/{pid}/conversations/{cid}：ConversationDto + messages。

    纯读；messages = 最近 50 条（先按 id DESC 取 50，再反转为 id 升序）；
    不存在 / 跨项目 / 非本 owner → 404 not_found {kind:"conversation", id:cid}。
    """
    _require_project(session, user, pid)
    conversation = _require_conversation(session, user, pid, cid)
    rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == cid)
        .order_by(Message.id.desc())
        .limit(_DETAIL_MESSAGE_LIMIT)
    ).all()
    messages = [_message_dto(m) for m in reversed(rows)]
    return dict(_conversation_dto(conversation), messages=messages)


# ────────────────────────── 4. 消息列表（GET） ──────────────────────────


def list_messages(session, user, pid: str, cid: str, cursor, limit) -> dict:
    """GET /projects/{pid}/conversations/{cid}/messages：200 {items, next_cursor}。

    纯读；按 id 升序，keyset cursor（id>cursor），缺省 50、上限 200；
    会话 404 形状与详情同；cursor 格式校验逐字对齐 P2。
    """
    _require_project(session, user, pid)
    _require_conversation(session, user, pid, cid)
    _check_cursor(cursor)
    limit = _clamp_limit(limit)

    stmt = select(Message).where(Message.conversation_id == cid)
    if cursor is not None:
        stmt = stmt.where(Message.id > cursor)
    rows = session.scalars(stmt.order_by(Message.id.asc()).limit(limit + 1)).all()
    items = [_message_dto(m) for m in rows[:limit]]
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


# ───────────────────────── 5. 追加消息（POST） ─────────────────────────


def post_message(
    session, user, pid: str, cid: str, sender, content, refs, job_id, command_id
) -> dict:
    """POST /projects/{pid}/conversations/{cid}/messages：201 MessageDto。

    校验序（按序短路）：项目 404 → 会话 404（同形状）→ sender 枚举 422 →
    content 空白 422 / 超长 422（>8000）→ refs 形状 422 → job_id 存在且属本
    项目，否则 404 {kind:"job", id:job_id} → 幂等（同 command_id 同内容 →
    200 原 payload；异内容 → 422 reused）→ 单事务 INSERT Message +
    UPDATE conversation.updated_at + INSERT CommandRecord。

    消息只追加（无 UPDATE/DELETE 语义）；content 原样保存（仅用 strip 判空）；
    refs 入库为 JSON 文本、出参解析为数组（null→null）。返回 MessageDto；
    重放命中时附带 ``_replay=True``（路由层 pop 后以 200 返回原 payload）。
    """
    _require_project(session, user, pid)
    conversation = _require_conversation(session, user, pid, cid)

    if sender not in _SENDERS:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "sender",
                    "rule": "enum",
                    "message": "sender 必须是 user 或 assistant。",
                }
            ]
        )
    if not content.strip():
        raise StudioAPIError.validation_failed(
            [{"field": "content", "rule": "required", "message": "消息内容不能为空。"}]
        )
    if len(content) > _MAX_CONTENT:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "content",
                    "rule": "too_long",
                    "message": f"消息内容最长 {_MAX_CONTENT} 个字符。",
                }
            ]
        )

    refs_norm = _normalize_refs(refs)

    if job_id is not None:
        job = session.scalar(
            select(StudioJob.id).where(
                StudioJob.id == job_id, StudioJob.project_id == pid
            )
        )
        if job is None:
            raise StudioAPIError.not_found("job", job_id)

    record = _load_command(session, user, command_id)
    if record is not None:
        payload = _payload_of(record)
        same = (
            payload.get("conversation_id") == cid
            and payload.get("sender") == sender
            and payload.get("content") == content
            and payload.get("refs") == refs_norm
            and payload.get("job_id") == job_id
        )
        if not same:
            raise _reused_error()
        return dict(payload, _replay=True)

    now = time.time()
    message = Message(
        id=make_ulid(),
        conversation_id=cid,
        sender=sender,
        content=content,
        refs=(
            json.dumps(refs_norm, ensure_ascii=False) if refs_norm is not None else None
        ),
        job_id=job_id,
        created_at=now,
    )
    dto = _message_dto(message)
    command = CommandRecord(
        id=make_ulid(),
        owner_id=user.id,
        project_id=pid,
        command_id=command_id,
        result_payload=json.dumps(dto, ensure_ascii=False),
        created_at=now,
    )
    session.commit()  # 结束只读事务；写阶段另起事务（UPDATE+INSERT 同事务）
    try:
        with session.begin():
            conversation.updated_at = now  # UPDATE 与两 INSERT 同事务
            session.add(message)
            session.flush()  # 先持久化父行：UOW 不保证跨表 FK 插入序
            session.add(command)
    except IntegrityError:
        # 并发竞态兜底（UQ owner+command）：回滚后按幂等语义重判，不半写
        session.rollback()
        return _recover_post(
            session, user, pid, cid, sender, content, refs_norm, job_id, command_id
        )
    return dto


def _recover_post(
    session, user, pid, cid, sender, content, refs_norm, job_id, command_id
) -> dict:
    """追加消息事务因唯一约束失败后的确定性兜底（不产生半写）。"""
    record = _load_command(session, user, command_id)
    if record is not None:
        payload = _payload_of(record)
        same = (
            payload.get("conversation_id") == cid
            and payload.get("sender") == sender
            and payload.get("content") == content
            and payload.get("refs") == refs_norm
            and payload.get("job_id") == job_id
        )
        if not same:
            raise _reused_error()
        return dict(payload, _replay=True)
    raise StudioAPIError.internal()
