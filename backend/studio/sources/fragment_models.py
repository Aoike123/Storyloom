"""Studio 片段三表模型（附录 02 §1 冻结）。

要点：
- fragment id 稳定：改名/边界/激活切换均不换 ID（实体 ID 与版本分离）；
- 持久 state 只有 candidate/confirmed/retired；pending_review 是派生值
  （绑定版本不再是当前正文时的 R11“需复核”标记），不落库；
- 候选与确认共同参与重叠校验，边界相接合法（重叠判定在服务层事务内
  全量比对，附录 00 §7）；
- 片段版本不可变，边界/拆合各产生一行，版本更新不换实体 ID；
- FK 环：studio_fragments.current_revision_id ↔ studio_fragment_revisions.fragment_id
  双向 NOT NULL，用 SQLAlchemy ForeignKey(..., deferrable=True, initially="DEFERRED")
  解决（SQLite `DEFERRABLE INITIALLY DEFERRED`：事务内两行先后插入、commit 时校验；
  注意裸 `DEFERRABLE` 在 SQLite 是 INITIALLY IMMEDIATE，语句末即校验，不满足
  该语义。这是 SQLite DML 顺序的实现细节，不改附录冻结的列/约束语义）；
- SQLite 映射：text(26)→String(32)、timestamptz→Float、JSON 数组→Text。
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


class RangeSet(Base):
    """studio_range_sets：范围集合 CAS 的唯一写入点（惰性创建，布局变更 +1）。"""

    __tablename__ = "studio_range_sets"
    __table_args__ = (
        UniqueConstraint("project_id", "source_revision_id", name="uq_studio_range_sets_project_revision"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    source_revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_source_revisions.id"), index=True
    )
    cas_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Fragment(Base):
    """studio_fragments：稳定实体；持久 state ∈ candidate/confirmed/retired（pending_review 派生，不落库）。"""

    __tablename__ = "studio_fragments"
    __table_args__ = (
        CheckConstraint("state IN ('candidate','confirmed','retired')", name="ck_studio_fragments_state"),
        Index("ix_studio_fragments_proj_rev_state", "project_id", "source_revision_id", "state"),
        Index("ix_studio_fragments_range_set", "range_set_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"))
    source_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_source_revisions.id"))
    range_set_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_range_sets.id"))
    name: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="candidate")
    current_revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_fragment_revisions.id", deferrable=True, initially="DEFERRED")
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    retired_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class FragmentRevision(Base):
    """studio_fragment_revisions：不可变版本行（UTF-16 半开区间 [range_start, range_end)，CHECK end>start）。"""

    __tablename__ = "studio_fragment_revisions"
    __table_args__ = (
        UniqueConstraint("fragment_id", "revision", name="uq_studio_fragment_revisions_frag_rev"),
        CheckConstraint("range_end > range_start", name="ck_studio_fragment_revisions_range"),
        CheckConstraint("reason IN ('created','boundary','split','merge')", name="ck_studio_fragment_revisions_reason"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    fragment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_fragments.id", deferrable=True, initially="DEFERRED"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    source_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_source_revisions.id"))
    range_start: Mapped[int] = mapped_column(Integer)
    range_end: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(12))
    predecessor_fragment_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
