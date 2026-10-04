"""Studio 发布表 studio_releases（附录 11 §1/§3 冻结，契约 v2.4）。

- 同名再发布 = 新行（predecessor_release_id 指向旧行）+ 旧行退役保留可看
  （R12 同名版本链）；
- 条件唯一（v2.4 修正）：每 (project_id, name) 至多一个存活
  (draft/published) 行——SQLite partial unique index
  (sqlite_where: status IN ('draft','published'))；plain UNIQUE 与 R12 矛盾
  （再发布后两行同名必然并存），故只约束存活行；
- retire = 状态变更，不删 public 文件（R12）；
- poster_artifact_id = 项目封面来源（项目无封面字段，可空）；
- 发布固定确认剪辑版本（confirmed_edit_id，不可漂移）。
"""
import time

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class Release(Base):
    """studio_releases 表：发布与同名版本链（条件唯一）。"""

    __tablename__ = "studio_releases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','retired')",
            name="ck_studio_releases_status",
        ),
        Index(
            "uq_studio_releases_project_name_live",
            "project_id",
            "name",
            unique=True,
            sqlite_where=text("status IN ('draft','published')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    confirmed_edit_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_confirmed_edits.id"), index=True
    )
    poster_artifact_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_media_artifacts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="draft")
    predecessor_release_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_releases.id"), nullable=True
    )
    public_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    cas_revision: Mapped[int] = mapped_column(Integer, default=1)
