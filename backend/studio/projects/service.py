"""Storyloom Studio — 项目域服务层（附录 01 §2 命令的服务端实现）。

SOURCE-01-a..e 逐子卡填充（每卡只实现其唯一命令）：
P1 新建 / P3 读取 / P2 列表 /（来源侧 S1/S3 在 sources/service.py）。

实现约束（冻结）：
- 所有写命令走 command_id 幂等（附录 00 §3，studio_command_records）；
- 属主校验：未登录 401；跨属主/不存在一律 404 not_found（不泄漏存在性，
  SOURCE-01 验收裁定：读写统一 404）；
- id 全部服务端 ULID（core.db.make_ulid）；
- 错误一律 raise StudioAPIError（冻结错误协议）。
"""
import json
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from .models import StudioProject

__all__ = ["create_project", "get_project"]


def _to_dto(p: StudioProject) -> dict:
    """项目 DTO（P1 成功/重放返回体；created_at/updated_at 取模型 created/updated 数值）。"""
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "visibility": p.visibility,
        "active_source_revision_id": p.active_source_revision_id,
        "created_at": p.created,
        "updated_at": p.updated,
    }


def create_project(session, user, name, description, command_id) -> dict:
    """P1 新建项目（附录 01 §2，契约 v2.6）。

    事务边界（自管）：
    - 幂等 / 属主内重名预检为读查询，先于写事务；
    - 重放路径不产生任何写、不 commit；
    - 写阶段（项目 + 命令记录）同一事务；IntegrityError（UQ 兜底/竞态）
      → 回滚并 raise duplicate。

    返回项目 DTO；重放命中时附带内部标记 ``_replay=True``（路由层 pop 后
    以 200 返回原 result_payload，不改变响应体形状）。
    """
    name_s = name.strip()

    # 校验（按序：先 required 后 max_length）；保存用 trimmed，description 原样
    violations: list[dict[str, str]] = []
    if not name_s:
        violations.append(
            {"field": "name", "rule": "required", "message": "项目名不能为空。"}
        )
    if len(name_s) > 80:
        violations.append(
            {"field": "name", "rule": "max_length", "message": "项目名最长 80 个字符。"}
        )
    if violations:
        raise StudioAPIError.validation_failed(violations)

    # 幂等（附录 00 §3）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if payload.get("name") == name_s and payload.get("description") == description:
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

    # 属主内重名（v2.6 新码 duplicate，409，details.scope_ref）
    dup = session.scalar(
        select(StudioProject).where(
            StudioProject.owner_id == user.id,
            StudioProject.name == name_s,
        )
    )
    session.commit()  # 结束读事务；写阶段另起事务
    if dup is not None:
        raise StudioAPIError.duplicate(
            f"projects:{user.id}:name:{name_s}",
            message="该项目名下已存在同名项目，请更换名称。",
        )

    now = time.time()
    project = StudioProject(
        id=make_ulid(),
        owner_id=user.id,
        name=name_s,
        description=description,  # 原样保存，不 trim
        visibility="private",
        active_source_revision_id=None,  # 不隐式导入任何来源（R12）
        created=now,
        updated=now,
    )
    command = CommandRecord(
        id=make_ulid(),
        owner_id=user.id,
        project_id=project.id,
        command_id=command_id,
        result_payload=json.dumps(_to_dto(project), ensure_ascii=False),
        created_at=now,
    )
    try:
        with session.begin():
            session.add(project)
            session.flush()  # 先持久化父行：UOW 不保证跨表 FK 插入序
            session.add(command)
    except IntegrityError:
        session.rollback()
        raise StudioAPIError.duplicate(
            f"projects:{user.id}:name:{name_s}",
            message="该项目名下已存在同名项目，请更换名称。",
        )

    return _to_dto(project)


def get_project(session, user, pid) -> dict:
    """P3 读取单项目（附录 01 §2，契约 v2.6）。

    纯读：不写任何表、不 commit、不接受 command_id（附录 00 §3）。
    不存在或非属主一律 404 not_found（不泄漏存在性，SOURCE-01 验收裁定）。
    """
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)
    return _to_dto(p)
