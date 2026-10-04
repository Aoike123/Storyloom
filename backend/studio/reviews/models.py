"""附录 07 §1 冻结结构：变更预览 / 审核决定。

要点：
- preview 不是隐式提交：创建预览零副作用；状态迁移只允许
  pending → applied|rejected|superseded|expired（终态不可逆，服务层强制）；
  superseded/expired 由服务惰性置位。
- 预览是过程记录：本两表允许 UPDATE（state/decision_id/resolved_at 回填），
  是模型层少数可变表。
- 一个预览至多一个终局决定（decision.preview_id UNIQUE，可空——SQLite 允许多 NULL）。
- 旧审核不能给新 revision 自动过关：决定绑定 baseline_digest（sha256 规范化
  baseline JSON）；新内容→新预览→新 digest→新决定，不做模糊继承。
- kind 冻结清单 11 种（前 9 种 ROOT-02 附录；edit_confirm/release_publish 为
  ROOT-03/04 占位 kind）。
- expires_at = created_at + 30 分钟（服务层计算后写入）。
- SQLite 映射：text(26)→String(32)、timestamptz→Float、JSON→Text。
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class ChangePreview(Base):
    """studio_change_previews 表：变更预览（过程记录，可 UPDATE 回填）。"""

    __tablename__ = "studio_change_previews"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('source_activate','fragment_retire','fragment_boundary',"
            "'fragment_split','fragment_merge','script_adopt','asset_version_adopt',"
            "'shot_generate','asset_generate','edit_confirm','release_publish')",
            name="ck_studio_change_previews_kind",
        ),
        CheckConstraint(
            "state IN ('pending','applied','rejected','superseded','expired')",
            name="ck_studio_change_previews_state",
        ),
        Index("ix_studio_change_previews_proj_state", "project_id", "state"),
        Index("ix_studio_change_previews_proj_kind", "project_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"))
    owner_id: Mapped[str] = mapped_column(String(80), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[str] = mapped_column(Text)
    baseline: Mapped[str] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(12), default="pending")
    decision_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_review_decisions.id"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    resolved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[float] = mapped_column(Float)


class ReviewDecision(Base):
    """studio_review_decisions 表：终局审核决定。

    AS7 资产版本审核 preview_id=NULL 也落一行决定
    （confirmed/rejected 名称映射在 API 层，表值冻结 applied/rejected）。
    """

    __tablename__ = "studio_review_decisions"
    __table_args__ = (
        UniqueConstraint("preview_id", name="uq_studio_review_decisions_preview"),
        CheckConstraint(
            "decision IN ('applied','rejected')",
            name="ck_studio_review_decisions_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(80), ForeignKey("users.id"), index=True)
    preview_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_change_previews.id"), nullable=True, index=True
    )
    target_ref: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(String(10))
    baseline_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
