"""P01 冻结结构；无 stage 列、无封面列（合同 v2.3：封面 = 最新已发布 Release 的 poster，见附录 11；stage 属旧系统概念，新系统不存在）。"""
import time

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class StudioProject(Base):
    """studio_projects 表：项目聚合根。

    FK 环（projects↔source_revisions）由 SQLite 在 DML 时解析，create_all 不受影响。
    """

    __tablename__ = "studio_projects"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_studio_projects_owner_name"),
        CheckConstraint(
            "visibility IN ('private','public')",
            name="ck_studio_projects_visibility",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    active_source_revision_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_source_revisions.id"), nullable=True
    )
    created: Mapped[float] = mapped_column(Float, default=time.time)
    updated: Mapped[float] = mapped_column(Float, default=time.time)
