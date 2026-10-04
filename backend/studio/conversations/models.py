"""附录 10 §1–§3 冻结模型（DB-09：studio_conversations / studio_messages / studio_proposals）。

要点：
- 每（项目,作用域,人）一个会话（UQ 四元组 project_id, scope_kind, scope_id, owner_id）。
- 消息与提案**只追加**（表无 UPDATE 语义列；提案 decided_at/status 由服务层一次性置位）。
- 提案应用 = 执行对应领域命令；必预览者只开 ChangePreview——
  **提案永不绕过预览、永不直写业务表**（R07/R11）。
- `superseded` = 同 kind+同目标 ref 仅一个 pending（服务层保证）。
- 消息 job_id 关联 StudioJob（进度可查）。
- SQLite 映射：ULID→String(32)、timestamptz→Float、JSON→Text。
"""
import time

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class Conversation(Base):
    """studio_conversations 表：每（项目, 作用域, 人）一个会话。"""

    __tablename__ = "studio_conversations"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "scope_kind",
            "scope_id",
            "owner_id",
            name="uq_studio_conversations_proj_scope_owner",
        ),
        CheckConstraint(
            "scope_kind IN ('project','fragment','scene','shot','asset','edit')",
            name="ck_studio_conversations_scope_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    owner_id: Mapped[str] = mapped_column(String(80), ForeignKey("users.id"), index=True)
    scope_kind: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Message(Base):
    """studio_messages 表：只追加对话消息（sender 仅 user/assistant）。"""

    __tablename__ = "studio_messages"
    __table_args__ = (
        CheckConstraint(
            "sender IN ('user','assistant')",
            name="ck_studio_messages_sender",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_conversations.id"), index=True
    )
    sender: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    refs: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_jobs.id"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Proposal(Base):
    """studio_proposals 表：只追加智能提案（23 种 kind 白名单）。"""

    __tablename__ = "studio_proposals"
    __table_args__ = (
        CheckConstraint(
            "kind IN ("
            "'source_import','fragment_rename','fragment_summary','fragment_split',"
            "'fragment_merge','fragment_boundary','fragment_retire','script_adopt',"
            "'scene_create','shot_create','shot_update','shot_reorder','shot_generate',"
            "'asset_create','asset_new_revision','asset_review','asset_version_adopt',"
            "'binding_add','binding_change','edit_instance_add','edit_instance_remove',"
            "'export','release_publish'"
            ")",
            name="ck_studio_proposals_kind",
        ),
        CheckConstraint(
            "status IN ('pending','adopted','rejected','superseded','expired')",
            name="ck_studio_proposals_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_conversations.id"), index=True
    )
    message_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_messages.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    decision_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    decided_at: Mapped[float | None] = mapped_column(Float, nullable=True)
