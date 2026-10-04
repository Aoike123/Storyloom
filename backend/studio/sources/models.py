"""S 系列冻结结构；不可变修订链（v1 无 UPDATE/DELETE 语义，由服务层保证）；canonical 文本 = CRLF/CR→LF；char_length = UTF-16 单位数；offset_policy 冻结 'lf-utf16-v1'。

ProductionScope（完整制作范围，R04）冻结于本文件（附录 02 规定位置）：
项目当前范围 = 同 project+revision 下 created_at 最新一行；历史只追加。
"""
import time

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class SourceRevision(Base):
    """studio_source_revisions 表：不可变来源修订。"""

    __tablename__ = "studio_source_revisions"
    __table_args__ = (
        CheckConstraint(
            "offset_policy = 'lf-utf16-v1'",
            name="ck_studio_sources_offset_policy",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    previous_revision_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_source_revisions.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), default="原始文稿")
    raw_content: Mapped[str] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(String(64))
    canonical_content: Mapped[str] = mapped_column(Text)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    offset_policy: Mapped[str] = mapped_column(String(32), default="lf-utf16-v1")
    char_length: Mapped[int] = mapped_column(Integer)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class ProductionScope(Base):
    """studio_production_scopes 表：完整制作范围（R04，附录 02 §1）。

    项目当前范围 = 同 project+source_revision 下 created_at 最新一行；
    历史只追加，不修改不删除。fragment_ids/excluded_ranges 为 JSON 数组文本。
    """

    __tablename__ = "studio_production_scopes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    source_revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_source_revisions.id"), index=True
    )
    fragment_ids: Mapped[str] = mapped_column(Text, default="[]")
    covered: Mapped[bool] = mapped_column(Boolean)
    excluded_ranges: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
