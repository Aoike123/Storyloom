"""StudioJob 任务/尝试/事件三表（附录 09 §1 冻结）。

StudioJob 是新流程唯一状态权威——进度/状态/计费/恢复全部来自这三张表；
无旧 Task/stage/node/读者分支任何字段；事件只追加（表无 UPDATE 语义列，
由服务层保证不 UPDATE/DELETE）。

SQLite 映射约定：附录中的 JSON 列 → Text 存 JSON 字符串（服务层序列化）；
TIMESTAMPTZ → Float epoch 秒（与本栈 created/updated 一致）。
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


class StudioJob(Base):
    """studio_jobs 表：任务主体，状态/租约/重试/usage 汇总。"""

    __tablename__ = "studio_jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('shot_generate','asset_generate','transcode','export','release_publish')",
            name="ck_studio_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')",
            name="ck_studio_jobs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_projects.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(80), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    ref_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    submitted_by: Mapped[str | None] = mapped_column(String(80), ForeignKey("users.id"), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    usage: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class JobAttempt(Base):
    """studio_job_attempts 表：每次尝试的租约/时间/厂商号/usage/cost/错误尾。"""

    __tablename__ = "studio_job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_studio_job_attempts_job_seq"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled','indeterminate')",
            name="ck_studio_job_attempts_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_jobs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    lease_owner: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_request_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    usage: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_tail: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobEvent(Base):
    """studio_job_events 表：只追加；列集合即全量（无 update 语义列）。"""

    __tablename__ = "studio_job_events"
    __table_args__ = (
        CheckConstraint(
            "type IN ('created','claimed','progress','provider_submitted','succeeded','failed','cancelled','indeterminate')",
            name="ck_studio_job_events_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("studio_jobs.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    at: Mapped[float] = mapped_column(Float)
    detail: Mapped[str] = mapped_column(Text, default="{}")
