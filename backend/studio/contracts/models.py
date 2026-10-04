"""命令幂等记录（附录 00 §3）；唯一约束 (owner_id, command_id)；result_payload 存首次成功的完整响应体（JSON 文本）；重放语义由服务层实现（模型只存）。"""
import time

from sqlalchemy import Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class CommandRecord(Base):
    """studio_command_records 表：命令幂等记录。"""

    __tablename__ = "studio_command_records"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "command_id", name="uq_studio_command_records_owner_command"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), ForeignKey("users.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), nullable=True
    )
    command_id: Mapped[str] = mapped_column(String(128))
    result_payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
