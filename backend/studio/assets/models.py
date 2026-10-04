"""附录 05 §1 冻结：资产/资产版本两表模型。

要点：
- 资产身份**项目内共用**：同一 ID 被多片段/多场引用，不按片段复制；
- 新版本不新建身份；**确认新版本不让任何场/镜头自动采用**（镜头引用 binding 固定的版本，附录 06；已发布快照不变，R07）；
- `review_status` 只表达**作者确认**这一个事实（结构校验/AI 预审/任务完成是其他机制，不混入本列，R07）；
- costume 的 character_id 条件规则（type=costume 必填且指向 character；character/scene 必须 NULL）由服务层校验，模型层只提供自引用 FK；
- **FK 环**：assets.current_revision_id ↔ asset_revisions.asset_id 双向 NOT NULL，用 `ForeignKey(..., deferrable=True, initially="DEFERRED")`（DB-02 冻结实现细节）；
- `media_artifact_id` 指向 studio_media_artifacts（DB-10 表，尚未创建）：SQLite 不校验 DDL 时父表存在，本卡测试中该列一律 NULL（DB-10 创建表前不得写非空值）；
- SQLite 映射：text(26)→String(32)、timestamptz→Float、JSON→Text、bool→Boolean。
"""
import time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class Asset(Base):
    """资产身份表（项目内共用，不按片段复制）。"""

    __tablename__ = "studio_assets"
    __table_args__ = (
        CheckConstraint(
            "type IN ('character','costume','scene')",
            name="ck_studio_assets_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    type: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(120))
    character_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_assets.id"), nullable=True, index=True
    )
    costume_required: Mapped[bool] = mapped_column(Boolean, default=True)
    current_revision_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(
            "studio_asset_revisions.id",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class AssetRevision(Base):
    """资产版本表（不可变，并存；(asset_id, revision) 唯一）。"""

    __tablename__ = "studio_asset_revisions"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "revision", name="uq_studio_asset_revisions_asset_rev"
        ),
        CheckConstraint(
            "review_status IN ('unreviewed','confirmed','rejected')",
            name="ck_studio_asset_revisions_review",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(
            "studio_assets.id",
            deferrable=True,
            initially="DEFERRED",
        ),
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    spec: Mapped[str] = mapped_column(Text)
    media_artifact_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_media_artifacts.id"), nullable=True
    )
    generation_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(12), default="unreviewed")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
