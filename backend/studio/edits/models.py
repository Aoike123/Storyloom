"""Studio 剪辑草稿/实例表 studio_edit_drafts / studio_edit_instances
（附录 11 §1/§2 冻结）。

- edit_id：长期稳定的剪辑对象 id（草稿与确认共享，跨版本不变）；
- 每次保存 = 新 edit_revision：manifest 快照 + digest 整版写入，
  UNIQUE(edit_id, edit_revision) 保证同版本唯一，写操作走 cas_revision CAS；
- 确认 = 冻结当前草稿 revision 的 manifest 原样（studio_confirmed_edits）：
  之后草稿继续可改，不影响已确认版本；
- studio_edit_instances = 草稿内实例的显式行（供 UI 单独编辑/删除引用）：
  明确插入才写行；未保存的拖动/调整 = 纯本地，不落行；
- 毫秒时间模型（ROOT-03）：source_in_ms / source_out_ms / timeline_start_ms 毫秒。
- ConfirmedEdit 归属本模块（DB-10 验收裁定：附录 11 §1 五表均属 DB-10，
  “确认不可变剪辑”聚合归剪辑域；卡面白名单遗漏已补）。
"""
import time

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class EditDraft(Base):
    """studio_edit_drafts 表：剪辑草稿版本链（每次保存 = 新 revision）。"""

    __tablename__ = "studio_edit_drafts"
    __table_args__ = (
        UniqueConstraint("edit_id", "edit_revision", name="uq_studio_edit_drafts_edit_rev"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    edit_id: Mapped[str] = mapped_column(String(32), index=True)
    edit_revision: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[str] = mapped_column(Text)  # EditManifest v1 JSON
    manifest_digest: Mapped[str] = mapped_column(String(64))
    cas_revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class EditInstance(Base):
    """studio_edit_instances 表：草稿内实例的显式行。"""

    __tablename__ = "studio_edit_instances"
    __table_args__ = (
        UniqueConstraint("draft_id", "instance_id", name="uq_studio_edit_instances_draft_instance"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    draft_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_edit_drafts.id"), index=True
    )
    instance_id: Mapped[str] = mapped_column(String(32))  # = manifest 内 instance id
    media_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_media_artifacts.id"), index=True
    )
    source_in_ms: Mapped[int] = mapped_column(Integer)
    source_out_ms: Mapped[int] = mapped_column(Integer)
    speed: Mapped[float] = mapped_column(Float)
    timeline_start_ms: Mapped[int] = mapped_column(Integer)
    track_id: Mapped[int] = mapped_column(Integer, default=0)


class ConfirmedEdit(Base):
    """studio_confirmed_edits 表：不可变确认版本（附录 11 §1/§2 冻结，R09）。

    确认 = 冻结当前草稿 revision 的 manifest 原样；之后草稿继续可改，
    不影响已确认版本。UNIQUE(edit_id, edit_revision)：同编辑同版本至多
    确认一次。发布（Release.confirmed_edit_id FK 本表行 PK）固定到具体
    确认版本行，而非稳定 edit_id 列（DB-10 验收裁定：规格字面为准）。
    """

    __tablename__ = "studio_confirmed_edits"
    __table_args__ = (
        UniqueConstraint("edit_id", "edit_revision", name="uq_studio_confirmed_edits_edit_rev"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    edit_id: Mapped[str] = mapped_column(String(32), index=True)
    edit_revision: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[str] = mapped_column(Text)  # 冻结 manifest 原样
    manifest_digest: Mapped[str] = mapped_column(String(64))
    output_spec: Mapped[str] = mapped_column(Text)  # JSON
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
