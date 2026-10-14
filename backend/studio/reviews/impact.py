"""跨域 impact 计算——F7/F8/F9/F10 预览共用。

确定性：各列表按 object_ref.id 排序；空表 → 空列表，键不省略；
缺表（当前 DB 未交付该表）→ ``OperationalError`` 捕获后该规则降级为空，绝不 500。

三类清单（附录 02 §2 impact 计算，主模型裁定）：
- affected[]：直接引用目标片段（或其后继组）的
  SourceRelation / AssetOccurrence（含引用时记录的 fragment revision）/
  ScriptObject / ScriptAdoption（revision=null）/ Shot（当前 revision 号，
  经其采用的 adoption 关联）；
- needs_review[]：冻结输入 payload 文本含任一 group id 且 status ∈ (pending,
  running) 的 StudioJob（附录 "queued" = 模型值 pending）；
- preservable[]：证据链 MediaArtifact(source_job_id∈job 组) → EditInstance
  (media_id) → Release(poster_artifact_id)。
"""
import json

from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import OperationalError

from ..jobs.models import StudioJob
from ..media.models import MediaArtifact
from ..relations.models import AssetOccurrence, SourceRelation
from ..scripts.models import ScriptAdoption, ScriptObject, ScriptRevision
from ..sources.fragment_models import Fragment, FragmentRevision

__all__ = ["compute_impact", "compute_script_adopt_impact"]

# 跨域只读查询（表名冻结常量；表可能尚未交付进当前 DB → 缺表时该规则降级为空，不 500）
_SHOT_SQL = (
    "SELECT s.id, r.revision FROM studio_shots s "
    "JOIN studio_shot_revisions r ON r.id = s.current_revision_id "
    "WHERE s.project_id = :pid AND s.script_adoption_id IN :adoptions"
)
_EDIT_INSTANCE_SQL = "SELECT id FROM studio_edit_instances WHERE media_id IN :ids"
# SC6 采用 impact 的 media 证据链起点（与 _EDIT_INSTANCE_SQL/_RELEASE_SQL 同款
# 跨域只读；表未交付 → _raw_rows 捕获 OperationalError 降级为空）
_MEDIA_ARTIFACT_SQL = (
    "SELECT id FROM studio_media_artifacts "
    "WHERE project_id = :pid AND source_job_id IN :ids"
)
_RELEASE_SQL = (
    "SELECT id FROM studio_releases WHERE project_id = :pid "
    "AND poster_artifact_id IN :ids"
)


def _raw_rows(session, sql, params) -> list:
    """跨域只读查询：params 中 list/set/tuple 值自动展开为 IN 列表；
    表不存在（当前 DB 未交付该表）→ 返回空列表（该 impact 规则降级），绝不 500。"""
    expanded = dict(params)
    for k, v in list(expanded.items()):
        if isinstance(v, (set, frozenset)):
            expanded[k] = sorted(v)
        elif isinstance(v, tuple):
            expanded[k] = list(v)
    q = text(sql)
    for k, v in expanded.items():
        if isinstance(v, list):
            q = q.bindparams(bindparam(k, expanding=True))
    try:
        return session.execute(q, expanded).all()
    except OperationalError:
        return []


def _successor_group(session, pid, fid) -> set:
    """后继组（主模型裁定）：{fid} ∪ 传递闭包——任何 FragmentRevision 行的
    predecessor_fragment_ids 含组内 id 的 fragment 全部入组（split/merge 后继链）。"""
    preds: dict[str, set] = {}
    for f in session.scalars(select(Fragment).where(Fragment.project_id == pid)).all():
        s: set = set()
        for r in session.scalars(
            select(FragmentRevision).where(FragmentRevision.fragment_id == f.id)
        ).all():
            s.update(json.loads(r.predecessor_fragment_ids or "[]"))
        preds[f.id] = s
    group = {fid}
    changed = True
    while changed:
        changed = False
        for f_id, s in preds.items():
            if f_id not in group and (s & group):
                group.add(f_id)
                changed = True
    return group


def compute_impact(session, pid, fid) -> dict:
    """F7–F10 预览共用 impact（附录 02 §2 impact 计算，主模型裁定；确定性 = 各列表按
    object_ref.id 排序；空表 → 空列表，键不得省略；缺表 OperationalError 降级为空）：
    - affected[]：直接引用后继组的 SourceRelation/AssetOccurrence（含引用时记录的
      fragment revision）/ScriptObject/ScriptAdoption（revision=null）/Shot（当前
      revision 号，经其采用的 adoption 关联）；
    - needs_review[]：冻结输入 payload 文本含任一 group id 且 status ∈ (pending,
      running) 的 StudioJob（附录 "queued" = 模型值 pending）；
    - preservable[]：证据链 MediaArtifact(source_job_id∈job 组) → EditInstance
      (media_id) → Release(poster_artifact_id)（跨域表未交付时降级为空）。
    """
    group = _successor_group(session, pid, fid)

    affected: list[dict] = []
    for r in session.scalars(
        select(SourceRelation).where(
            SourceRelation.project_id == pid,
            SourceRelation.source_fragment_id.in_(group),
        )
    ).all():
        affected.append(
            {
                "object_ref": {
                    "kind": "source_relation",
                    "id": r.id,
                    "revision": r.source_fragment_revision,
                }
            }
        )
    for a in session.scalars(
        select(AssetOccurrence).where(
            AssetOccurrence.project_id == pid,
            AssetOccurrence.source_fragment_id.in_(group),
        )
    ).all():
        affected.append(
            {
                "object_ref": {
                    "kind": "asset_occurrence",
                    "id": a.id,
                    "revision": a.source_fragment_revision,
                }
            }
        )
    for o in session.scalars(
        select(ScriptObject).where(ScriptObject.fragment_id.in_(group))
    ).all():
        orev = session.get(ScriptRevision, o.current_revision_id)
        affected.append(
            {
                "object_ref": {
                    "kind": "script_object",
                    "id": o.id,
                    "revision": orev.revision if orev is not None else None,
                }
            }
        )
    adoption_ids: set = set()
    for ad in session.scalars(
        select(ScriptAdoption).where(ScriptAdoption.fragment_id.in_(group))
    ).all():
        adoption_ids.add(ad.id)
        affected.append(
            {"object_ref": {"kind": "script_adoption", "id": ad.id, "revision": None}}
        )
    if adoption_ids:
        for row in _raw_rows(
            session, _SHOT_SQL, {"pid": pid, "adoptions": sorted(adoption_ids)}
        ):
            affected.append(
                {"object_ref": {"kind": "shot", "id": row[0], "revision": row[1]}}
            )
    affected.sort(key=lambda x: x["object_ref"]["id"])

    # job 组 = payload（冻结输入）文本含任一 group id 的全部项目 job（任意状态）；
    # needs_review = 该组 ∩ {pending, running}
    jobs = session.scalars(select(StudioJob).where(StudioJob.project_id == pid)).all()
    job_group = [j for j in jobs if any(g in (j.payload or "") for g in group)]
    needs_review: list[dict] = []
    for j in job_group:
        if j.status in ("pending", "running"):
            needs_review.append(
                {
                    "object_ref": {"kind": "job", "id": j.id, "revision": None},
                    "reason": "frozen_input_contains_fragment",
                }
            )
    needs_review.sort(key=lambda x: x["object_ref"]["id"])

    preservable: list[dict] = []
    if job_group:
        jids = [j.id for j in job_group]
        arts = session.scalars(
            select(MediaArtifact).where(
                MediaArtifact.project_id == pid,
                MediaArtifact.source_job_id.in_(jids),
            )
        ).all()
        for m in arts:
            preservable.append(
                {"object_ref": {"kind": "media_artifact", "id": m.id, "revision": None}}
            )
        aids = [m.id for m in arts]
        if aids:
            for row in _raw_rows(session, _EDIT_INSTANCE_SQL, {"ids": aids}):
                preservable.append(
                    {
                        "object_ref": {
                            "kind": "edit_instance",
                            "id": row[0],
                            "revision": None,
                        }
                    }
                )
            for row in _raw_rows(session, _RELEASE_SQL, {"pid": pid, "ids": aids}):
                preservable.append(
                    {"object_ref": {"kind": "release", "id": row[0], "revision": None}}
                )
    preservable.sort(key=lambda x: x["object_ref"]["id"])

    return {"affected": affected, "needs_review": needs_review, "preservable": preservable}


def compute_script_adopt_impact(session, pid, fid) -> dict:
    """SC6 采用预览 impact（附录 03 §2 SC6 + 00 §5；沿用本文件 _successor_group/
    _raw_rows；确定性 = 各列表按 object_ref.id 升序；空表 → 空列表，键不省略；
    跨域表未交付（OperationalError）→ 该规则降级为空，绝不 500）。

    - affected[]：`shot` —— `studio_shots.script_adoption_id` ∈ 该片段**后继组**
      的全部 adoption id（旧采用上的镜头输入冻结于具体 adoption；列出仅为提示，
      本次采用不改写任何已冻结输入）；
    - needs_review[]：`job` —— `project_id=pid`、`status ∈ (pending, running)` 且
      payload 文本含后继组任一 fragment id 或该组任一 adoption id；
      reason 固定 `frozen_input_references_previous_adoption`；
    - preservable[]：`media_artifact`（`source_job_id ∈ job 组`）→ 其
      `edit_instance`（`media_id ∈ 上述 media`）→ 其 `release`
      （`poster_artifact_id ∈ 上述 media`）；三者 revision 均 null。

    `job 组` 沿用 compute_impact 既有语义：payload 文本命中后继组 fragment id 或
    该组 adoption id 的**全部**项目 job（任意状态）；needs_review 是其与
    {pending, running} 的交集，preservable 取全组（已产出媒体多来自已结束 job）。
    """
    group = _successor_group(session, pid, fid)

    # 该片段后继组的全部 adoption id（旧采用；不区分是否链尾——历史上任何采用
    # 都可能被冻结引用）
    adoption_ids: set = {
        ad.id
        for ad in session.scalars(
            select(ScriptAdoption).where(ScriptAdoption.fragment_id.in_(group))
        ).all()
    }

    affected: list[dict] = []
    if adoption_ids:
        for row in _raw_rows(
            session, _SHOT_SQL, {"pid": pid, "adoptions": sorted(adoption_ids)}
        ):
            affected.append(
                {"object_ref": {"kind": "shot", "id": row[0], "revision": row[1]}}
            )
    affected.sort(key=lambda x: x["object_ref"]["id"])

    # job 组 = payload（冻结输入）文本含任一 group fragment id 或任一 group
    # adoption id 的项目 job（任意状态）；needs_review = 该组 ∩ {pending, running}
    needles = set(group) | adoption_ids
    try:
        jobs = session.scalars(
            select(StudioJob).where(StudioJob.project_id == pid)
        ).all()
    except OperationalError:  # 表未交付（当前 DB 未交付）→ 该规则降级为空
        jobs = []
    job_group = [j for j in jobs if any(n in (j.payload or "") for n in needles)]
    needs_review: list[dict] = []
    for j in job_group:
        if j.status in ("pending", "running"):
            needs_review.append(
                {
                    "object_ref": {"kind": "job", "id": j.id, "revision": None},
                    "reason": "frozen_input_references_previous_adoption",
                }
            )
    needs_review.sort(key=lambda x: x["object_ref"]["id"])

    preservable: list[dict] = []
    if job_group:
        jids = [j.id for j in job_group]
        mrows = _raw_rows(session, _MEDIA_ARTIFACT_SQL, {"pid": pid, "ids": jids})
        aids = [row[0] for row in mrows]
        for aid in aids:
            preservable.append(
                {"object_ref": {"kind": "media_artifact", "id": aid, "revision": None}}
            )
        if aids:
            for row in _raw_rows(session, _EDIT_INSTANCE_SQL, {"ids": aids}):
                preservable.append(
                    {
                        "object_ref": {
                            "kind": "edit_instance",
                            "id": row[0],
                            "revision": None,
                        }
                    }
                )
            for row in _raw_rows(session, _RELEASE_SQL, {"pid": pid, "ids": aids}):
                preservable.append(
                    {"object_ref": {"kind": "release", "id": row[0], "revision": None}}
                )
    preservable.sort(key=lambda x: x["object_ref"]["id"])

    return {"affected": affected, "needs_review": needs_review, "preservable": preservable}
