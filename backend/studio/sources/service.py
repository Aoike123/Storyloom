"""Storyloom Studio — 来源域服务层（附录 01 §2 S1–S6 的服务端实现）。

SOURCE-01-d/e 逐子卡填充（S1 导入不可变原文、S3 读取来源版本）；
S2/S4/S5/S6 属 SOURCE-03+ 后续卡。

实现约束（冻结）：
- raw 原样保存：不 trim、不 Unicode 归一化（附录 01 §1）；
- canonical = CRLF/CR→LF（其余不变）；哈希 = sha256(UTF-8)；
  char_length = canonical 的 UTF-16 code unit 数——三者必须用
  backend/studio/sources/ranges.py 的 canonicalize/utf16_len（附录 01 §4：
  与附录 00 §7 同源，共享 SOURCE-02 helper）；
- offset_policy 固定 'lf-utf16-v1'；无 UPDATE/DELETE 命令（R03/R11）；
- 幂等/属主/错误协议同 projects/service.py 头注。
"""
import hashlib
import json
import time

from sqlalchemy import select

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from .models import SourceRevision
from .ranges import canonicalize, utf16_len

__all__ = ["import_source"]

# 附录 01 §1：offset_policy 固定常量（响应必回）
OFFSET_POLICY = "lf-utf16-v1"

# 附录 01 §2 S1：未自动激活时的 activation_hint 文案（指向 S4/S5）
_ACTIVATE_HINT_MESSAGE = "已有活跃版本。切换到该版本需预览确认（S4/S5）。"


def import_source(session, user, pid, content, command_id) -> dict:
    """S1 导入不可变原文（附录 01 §2，契约 v2.6）。

    行为（冻结）：
    - 前置：项目不存在或非属主 → 一律 404 not_found
      （SOURCE-01 裁定：读写统一 404，不泄漏存在性）；
    - 校验：仅判空（content == ""）→ 422 validation_failed；
      全空白文本合法（保存后由范围层拒绝 blank 片段）；
    - 幂等（附录 00 §3，同 P1 模式）：(owner_id, command_id) 记录存在
      → 内容一致 = 重放：返回原 result_payload（零写零 commit，附带
      内部标记 ``_replay=True`` 由路由转 200）；不一致 →
      422 validation_failed（field=command_id, rule=reused）；
    - 创建（不可变，只追加）：raw 原样（不 trim、不归一化）；
      canonical = canonicalize(raw)；哈希 = sha256(UTF-8)；
      char_length = utf16_len(canonical)；offset_policy 固定常量；
      previous_revision_id = 该项目最新修订（ORDER BY created DESC,
      id DESC 首条；无修订 → None，线性导入史）；
    - 激活：项目 active 指针为空 → 设为新 id 并 updated=now
      （指针变更 = 项目变更）；已有 → 不动；
    - duplicate_content：项目已有任一修订 canonical_hash 相同 → True
      （恒含此键）；重复导入仍建新版本（历史依据，R03）。

    事务边界（自管）：
    - 幂等/历史链/重复判定为读查询，先于写事务；
    - 重放路径不产生任何写、不 commit；
    - 写阶段（新修订 + 项目指针 + 命令记录）同一事务；写序先 add
      新修订再 flush 后更新项目指针（项目行已存在于库，
      无跨表插入序问题）。

    返回修订 DTO（§1 全字段 + is_active + activation_hint +
    duplicate_content）；重放命中时附带内部标记 ``_replay=True``
    （路由层 pop 后以 200 返回原 result_payload，不改变响应体形状）。
    """
    # 前置：项目须存在且属主；否则一律 404（不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 校验：仅判空；全空白文本合法
    if content == "":
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "content",
                    "rule": "required",
                    "message": "原文不能为空。",
                }
            ]
        )

    # 幂等（附录 00 §3，同 P1 模式）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if payload.get("raw_content") == content:
            # 重放：返回首次 result_payload 解析出的 DTO，无副作用
            return dict(payload, _replay=True)
        # 同 command_id 不同内容（v2.6 裁定：冻结码表无专用码，validation_failed 承载）
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "command_id",
                    "rule": "reused",
                    "message": "command_id 已被其他命令使用，请生成新的。",
                }
            ]
        )

    # canonical / 哈希 / 长度（附录 01 §1/§4：与附录 00 §7 同源 helper）
    raw_content = content  # 原样：不 trim、不 Unicode 归一化
    canonical_content = canonicalize(raw_content)
    raw_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    canonical_hash = hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()

    # 历史链：最新修订 = ORDER BY created DESC, id DESC 首条
    prev_id = session.scalar(
        select(SourceRevision.id)
        .where(SourceRevision.project_id == pid)
        .order_by(SourceRevision.created.desc(), SourceRevision.id.desc())
        .limit(1)
    )
    # 重复判定：项目已有任一修订 canonical_hash 相同
    dup_row = session.execute(
        select(SourceRevision.id)
        .where(
            SourceRevision.project_id == pid,
            SourceRevision.canonical_hash == canonical_hash,
        )
        .limit(1)
    ).first()
    duplicate_content = dup_row is not None

    session.commit()  # 结束读事务；写阶段另起事务

    now = time.time()
    new_id = make_ulid()
    revision = SourceRevision(
        id=new_id,
        project_id=pid,
        previous_revision_id=prev_id,
        raw_content=raw_content,
        raw_hash=raw_hash,
        canonical_content=canonical_content,
        canonical_hash=canonical_hash,
        offset_policy=OFFSET_POLICY,
        char_length=utf16_len(canonical_content),
        created=now,
        # name 用模型默认（"原始文稿"），不进 DTO
    )

    # 激活：指针为空 → 首个导入自动激活；已有活跃版本 → 不动
    will_activate = project.active_source_revision_id is None
    hint = (
        None
        if will_activate
        else {
            "kind": "source_activate",
            "target_revision_id": new_id,
            "message": _ACTIVATE_HINT_MESSAGE,
        }
    )

    response = {
        "id": new_id,
        "project_id": pid,
        "previous_revision_id": prev_id,
        "raw_content": raw_content,
        "raw_hash": raw_hash,
        "canonical_content": canonical_content,
        "canonical_hash": canonical_hash,
        "offset_policy": OFFSET_POLICY,
        "char_length": revision.char_length,
        "created_at": now,
        "is_active": will_activate,
        "activation_hint": hint,
        "duplicate_content": duplicate_content,
    }

    # 写序：先 add 新修订再 flush，然后更新项目指针
    # （项目行已存在于库，无跨表插入序问题）；命令记录同事务
    with session.begin():
        session.add(revision)
        session.flush()
        if will_activate:
            project.active_source_revision_id = new_id
            project.updated = now
        session.add(
            CommandRecord(
                id=make_ulid(),
                owner_id=user.id,
                project_id=pid,
                command_id=command_id,
                result_payload=json.dumps(response, ensure_ascii=False),
                created_at=now,
            )
        )

    return response
