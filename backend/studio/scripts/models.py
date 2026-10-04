"""Studio 剧本三表模型（附录 03 §1 冻结）。

要点：
- 对象 id 稳定：一个对象唯一归属一个片段（fragment_id 唯一真实归属）；
- 无 stage/状态列：**草稿与采用是两个事实**——最新 revision 是工作草稿，
  ScriptAdoption 钉住被采用的 revision；对象本身不因此换 ID；
- revision 不可变，随内容冻结 speaker 与 source_fragment_revision（出处快照；
  片段边界变更后此处不变）；
- 采用不可变、链式（previous_adoption_id）；片段当前采用 = 链尾；
- FK 环：script_objects.current_revision_id ↔ script_revisions.object_id
  双向 NOT NULL，用 SQLAlchemy ForeignKey(..., deferrable=True, initially="DEFERRED")
  解决（SQLite 裸 DEFERRABLE = INITIALLY IMMEDIATE，语句末即校验，不满足
  该语义；必须显式 DEFERRED，事务内两行先后插入、commit 时才校验）。
  这是 DB-02 冻结的实现细节（与 fragments↔fragment_revisions 一致）；
- SQLite 映射：text(26)→String(32)、timestamptz→Float、JSON→Text。
"""
import time

from sqlalchemy import (
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


class ScriptObject(Base):
    """studio_script_objects：剧本对象，id 稳定、唯一归属一个片段；无 stage/状态列（草稿与采用是两个事实）。"""

    __tablename__ = "studio_script_objects"
    __table_args__ = (
        UniqueConstraint("fragment_id", "seq", name="uq_studio_script_objects_frag_seq"),
        CheckConstraint("kind IN ('action','dialogue')", name="ck_studio_script_objects_kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    fragment_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_fragments.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    seq: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str | None] = mapped_column(String(80), nullable=True)
    current_revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_script_revisions.id", deferrable=True, initially="DEFERRED")
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    retired_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ScriptRevision(Base):
    """studio_script_revisions：不可变版本行（随内容冻结 speaker 与 source_fragment_revision 出处快照）。"""

    __tablename__ = "studio_script_revisions"
    __table_args__ = (
        UniqueConstraint("object_id", "revision", name="uq_studio_script_revisions_obj_rev"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    object_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_script_objects.id", deferrable=True, initially="DEFERRED"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(String(80), nullable=True)
    adaptation_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_fragment_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ScriptAdoption(Base):
    """studio_script_adoptions：片段级采用绑定（不可变、链式；链尾 = 片段当前采用）。"""

    __tablename__ = "studio_script_adoptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    fragment_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_fragments.id"), index=True)
    object_revisions: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    previous_adoption_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_script_adoptions.id"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
