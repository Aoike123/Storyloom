"""Studio 分镜三表模型（附录 04 §1 冻结）。

要点：
- 镜头**唯一父级**：属于且只属于一场（scene_id 唯一真实归属，无第二归属）；
  本轮无删除/退役命令（未采用前只是草稿容器，不影响任何下游）；
- 镜头依据的采用剧本固定到具体 adoption（script_adoption_id），出处 = 其片段；
- 正式顺序由 seq 表达，**坐标不推断顺序**；场/镜头布局重排走 layout_cas_revision
  （唯一 CAS 写入点，对标 range_set）；
- 镜头版本不可变；采用剧本/资产不存副本——输入 = script_adoption + AssetBinding
  （附录 06）+ 当前 revision，生成任务提交时把这些冻结进任务（ROOT-04），此处
  只保证引用可解析；
- FK 环：shots.current_revision_id ↔ shot_revisions.shot_id 双向 NOT NULL，
  用 SQLAlchemy ForeignKey(..., deferrable=True, initially="DEFERRED") 解决
  （DB-02 冻结实现细节，与 fragments↔fragment_revisions 一致；SQLite 裸
  `DEFERRABLE` = INITIALLY IMMEDIATE，语句末即校验，不满足该语义；必须显式
  DEFERRED，事务内两行先后插入、commit 时才校验）；
- SQLite 映射：text(26)→String(32)、timestamptz→Float。
"""
import time

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class Scene(Base):
    """studio_scenes：场聚合；(project_id, seq) 唯一；layout_cas_revision 是镜头/场布局唯一 CAS 写入点。"""

    __tablename__ = "studio_scenes"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_studio_scenes_project_seq"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    seq: Mapped[int] = mapped_column(Integer)
    layout_cas_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Shot(Base):
    """studio_shots：镜头；唯一父级 scene_id；(scene_id, seq) 唯一；坐标不推断顺序。"""

    __tablename__ = "studio_shots"
    __table_args__ = (
        UniqueConstraint("scene_id", "seq", name="uq_studio_shots_scene_seq"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    scene_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_scenes.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    script_adoption_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_script_adoptions.id"), index=True
    )
    current_revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_shot_revisions.id", deferrable=True, initially="DEFERRED")
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class ShotRevision(Base):
    """studio_shot_revisions：不可变版本行；(shot_id, revision) 唯一，revision 自 1 起。"""

    __tablename__ = "studio_shot_revisions"
    __table_args__ = (
        UniqueConstraint("shot_id", "revision", name="uq_studio_shot_revisions_shot_rev"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shot_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_shots.id", deferrable=True, initially="DEFERRED"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    visual: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    dialogue: Mapped[str | None] = mapped_column(Text, nullable=True)
    transition_in: Mapped[str | None] = mapped_column(String(60), nullable=True)
    transition_out: Mapped[str | None] = mapped_column(String(60), nullable=True)
    duration_hint_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
