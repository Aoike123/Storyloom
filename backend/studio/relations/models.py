"""附录 06 §1 冻结：出处关系 / 资产出现 / 生成绑定三表模型。

要点：
- source_relations 只存**额外显式链接**（主出处是归属列/adoption 的派生，不占行）；
- 出现的范围 (range_start, range_end) **成对可空**：全空 = "无精确范围"桶，
  同一桶内唯一（R05：出现是出处，不复制资产/不建片段/不承担归属）；
- binding 固定具体 asset_revision（资产新版本不自动改绑定）；target_character_id
  仅 purpose=visual 且资产 type=costume 时必填（条件必填由服务层校验）；
- **双服装强校验**（同 shot 同 target_character 最多一条 costume visual 绑定）
  是服务层 precondition（double_costume），模型层只提供数据；
- 镜头生成输入 = script_adoption + 全部 binding，任务提交时冻结进 StudioJob（ROOT-04）；
- SQLite 映射：text(26)→String(32)、timestamptz→Float。
"""
import time

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    column as sa_column,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ...core.db import Base


class SourceRelation(Base):
    """studio_source_relations：额外显式出处链接（非主出处）。"""

    __tablename__ = "studio_source_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_fragment_id", "target_kind", "target_id",
            name="uq_studio_source_relations_src_tgt",
        ),
        CheckConstraint(
            "target_kind IN ('script_object','shot')",
            name="ck_studio_source_relations_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    source_fragment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_fragments.id"), index=True
    )
    source_fragment_revision: Mapped[int] = mapped_column(Integer)
    target_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    target_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class AssetOccurrence(Base):
    """studio_asset_occurrences：资产在原文片段中的出现（出处，不复制/不建片段）。

    NULL 桶唯一：range_start/range_end 成对可空；全空 = "无精确范围"桶，
    同一 (asset, fragment) 下该桶唯一。SQLite 的 UNIQUE 视 NULL 互异，
    故用 coalesce 表达式索引（Index(..., unique=True)）保证 NULL 桶唯一。
    """

    __tablename__ = "studio_asset_occurrences"
    __table_args__ = (
        Index(
            "uq_studio_asset_occurrences_asset_frag_range",
            "asset_id",
            "source_fragment_id",
            func.coalesce(sa_column("range_start"), -1),
            func.coalesce(sa_column("range_end"), -1),
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_assets.id"), index=True
    )
    asset_revision: Mapped[int] = mapped_column(Integer)
    source_fragment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_fragments.id"), index=True
    )
    source_fragment_revision: Mapped[int] = mapped_column(Integer)
    range_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    range_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class AssetBinding(Base):
    """studio_asset_bindings：镜头生成输入绑定（固定具体 asset_revision）。

    唯一 (shot_id, asset_id, purpose, target_character_id)；
    target_character_id 可空（NULL 桶用 coalesce 表达式索引保证唯一性）。
    双服装强校验是服务层 precondition，模型层只提供数据。
    """

    __tablename__ = "studio_asset_bindings"
    __table_args__ = (
        Index(
            "uq_studio_asset_bindings_shot_asset_purpose",
            "shot_id",
            "asset_id",
            "purpose",
            func.coalesce(sa_column("target_character_id"), ""),
            unique=True,
        ),
        CheckConstraint(
            "purpose IN ('visual','background','prop')",
            name="ck_studio_asset_bindings_purpose",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_projects.id"), index=True
    )
    shot_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_shots.id"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("studio_assets.id"), index=True
    )
    asset_revision: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(12))
    target_character_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("studio_assets.id"), nullable=True
    )
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
