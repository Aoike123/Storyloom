"""Studio 媒体产物表 studio_media_artifacts（附录 11 §1 冻结，契约 v2.4）。

- filename：私有存储内相对名（附录 08 §4：data/studio/media/{pid}/），不落绝对路径；
- sha256：内容可追溯（完整性校验/去重）；
- source_job_id：关联产生它的 StudioJob（nullable——upload 等无 job 来源）；
- duration_ms/width/height：媒体维度（毫秒时间模型 ROOT-03；image 无 duration）。

跨卡 FK 背景（DB-05 验收裁定）：本表交付前，assets/edits 的 FK 目标在
tests/studio/conftest.py 以最小占位 Table 注册（NoReferencedTableError
机制）；DB-10 验收时主模型已移除占位，本类为该表的唯一声明者。
"""
import time

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class MediaArtifact(Base):
    """studio_media_artifacts 表：私有存储内媒体产物登记。"""

    __tablename__ = "studio_media_artifacts"
    __table_args__ = (
        CheckConstraint(
            "media_kind IN ('image','video','audio')",
            name="ck_studio_media_artifacts_kind",
        ),
        CheckConstraint(
            "source IN ('upload','asset_generate','shot_generate','vendor_normalized','export')",
            name="ck_studio_media_artifacts_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    media_kind: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(24))
    source_job_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_jobs.id"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
