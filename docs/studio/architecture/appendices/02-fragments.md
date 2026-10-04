# 附录 02 · 范围集合、片段与片段版本（DB-02）

版本：2.2 · ROOT-02 冻结 · 2026-10-04（v2.7：F4/F5 失败码笔误修正——revision_conflict 依附录 00 §4 恒为 409）。覆盖：`RangeSet`、`Fragment`、`FragmentRevision`、`ProductionScope` 与 `/fragments`、`/production-scopes` 命令族。共享约定见[附录 00](00-object-ref-and-protocol.md)；字符契约执行其 §7。

实施位置：表 `backend/studio/sources/fragment_models.py`（ProductionScope 表在 `sources/models.py`，DB-01 卡）；命令 `backend/studio/sources/{fragment_service,fragments_api}.py`、`changes.py/changes_api.py`、`scopes.py/scopes_api.py`；测试 `tests/studio/test_fragment_models.py`、`test_fragments.py`、`test_fragment_changes.py`、`test_source_changes.py`、`test_scopes.py`。

## 1. 表

### studio_range_sets（集合 CAS 的唯一写入点）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK→studio_projects.id | |
| source_revision_id | text(26) | NOT NULL，FK→studio_source_revisions.id | |
| cas_revision | int | NOT NULL，默认 1 | 每次改变范围布局的写入 +1 |
| created_at | timestamptz | NOT NULL | |

- 唯一 `(project_id, source_revision_id)`；**惰性创建**（首个片段写入时，同事务内）。
- 重叠约束无法用普通索引表达，由**事务内全量比对**执行：事务对该行加写锁 → 读取同集合全部未退役片段 → 比对 → 写入 → `cas_revision+1`（附录 00 §3 保证并发下至多一个成功）。

### studio_fragments

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID，**稳定** | 改名/边界/激活切换均不换 ID |
| project_id | text(26) | NOT NULL，FK | |
| source_revision_id | text(26) | NOT NULL，FK | 片段只属于一个具体正文版本 |
| range_set_id | text(26) | NOT NULL，FK→studio_range_sets.id | |
| name | text(120) | NOT NULL | 就地改名，不产生 revision |
| summary | text(300) | NULL | 就地更新（用户或 AI 提案采用后），不产生 revision |
| state | text(16) | NOT NULL，CHECK IN ('candidate','confirmed','retired')，默认 'candidate' | 持久状态只有三种 |
| current_revision_id | text(26) | NOT NULL，FK→studio_fragment_revisions.id | |
| created_at / updated_at | timestamptz | NOT NULL | |
| retired_at | timestamptz | NULL | 退役时间；退役只置状态，不删除 |

- 索引：`(project_id, source_revision_id, state)`、`(range_set_id)`。
- **有效状态（DTO 的 `state` 字段，读时推导）**：`fragment.source_revision_id == project.active_source_revision_id` → 返回持久 state；否则若持久 state ∈ (candidate, confirmed) → 返回 `'pending_review'`；retired 恒为 `'retired'`。DTO 同时回 `revision_is_active: bool`。**pending_review 不落库**：它是“绑定版本不再是当前正文”的派生标记（R11“需复核”），重新激活原版本后自动消失，不存在状态回写或丢失问题。
- 任何写命令要求 `fragment.source_revision_id == active`，否则 `precondition_failed`（原因 `source_revision_inactive`）。
- **候选与确认共同参与重叠校验**（R04）；边界相接合法。

### studio_fragment_revisions

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| fragment_id | text(26) | NOT NULL，FK | |
| revision | int | NOT NULL，自 1 起 | 唯一 `(fragment_id, revision)` |
| source_revision_id | text(26) | NOT NULL，FK | 与片段一致（冗余，便于按版本查询） |
| range_start / range_end | int | NOT NULL，CHECK `end>start` | UTF-16 半开 `[start,end)` |
| reason | text(12) | NOT NULL，CHECK IN ('created','boundary','split','merge') | |
| predecessor_fragment_ids | text (JSON 数组) | NOT NULL，默认 `[]` | 仅 split/merge 的新实体填写来源片段 ID |
| created_at | timestamptz | NOT NULL | |

- 不可变；边界/拆合各产生一行。**实体 ID 与版本分离**：版本更新不换实体 ID。
- 片段的“当前范围”= `current_revision_id` 所指行的 (range_start, range_end)。

### studio_production_scopes（完整制作范围，R04）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK | |
| source_revision_id | text(26) | NOT NULL，FK | 范围绑定具体正文版本 |
| fragment_ids | text (JSON 数组) | NOT NULL | 快照：本次确认纳入制作的片段 ID |
| covered | bool | NOT NULL | 全部非空白码元是否都被范围片段覆盖 |
| excluded_ranges | text (JSON 数组 `[{start,end}]`) | NOT NULL | 范围外、非空白的最大连续段 |
| created_at | timestamptz | NOT NULL | |

- 项目当前范围 = 该 project+revision 下最新一行（`created_at` 倒序第一）；历史只追加。
- 独立于候选创建：增量候选允许未覆盖正文；本命令是“完整制作范围”的明确确认（R04）。

## 2. 命令（`/api/studio/projects/{pid}/...`）

所有写命令含 `command_id`；CAS 字段按表。preview/apply 遵循附录 00 §5（ChangePreview 结构见附录 07）。

| # | 方法 路径 | 请求（关键字段） | 成功 | 失败（code） |
| --- | --- | --- | --- | --- |
| F1 | POST `/fragments` | `source_revision_id, range{start,end}, name, expected_range_set_revision\|null, command_id` | 201 `{object_ref, name, summary, state:'candidate', range, created_at}`；同事务建/校验 range_set 并 +1 cas | 422 `range_invalid`/`validation_failed`(name)；409 `range_overlap`(conflicts) / `revision_conflict`(range_set cas)；422 `precondition_failed`(版本非 active) |
| F2 | GET `/fragments?cursor=&state=` | — | 200 列表（active 版本，`state` 为有效状态） | 404 |
| F3 | GET `/fragments/{fid}` | — | 200 DTO：object_ref、name、summary、state(有效)、revision_is_active、range、source_revision_id、predecessor_ids、created/updated/retired_at | 404 |
| F4 | POST `/fragments/{fid}/rename` | `name, expected_revision, command_id` | 200 DTO（ID 不变） | 409 `revision_conflict` / 422 `validation_failed` |
| F5 | POST `/fragments/{fid}/summary` | `summary\|null, expected_revision, command_id` | 200 DTO | 同上 |
| F6 | POST `/fragments/{fid}/confirm` | `expected_revision, command_id` | 200 state=confirmed。**不启动生产、不表示全文覆盖**（R04） | 422 `precondition_failed`(非 candidate)；409 `revision_conflict` |
| F7 | POST `/fragments/{fid}/retire` → **preview**；`POST /fragments/{fid}/retire/apply` | preview:`{target_fragment_id, command_id}`；apply:`{preview_id, command_id, expected_revision, expected_range_set_revision}` | apply 200：state=retired、retired_at；**不删除任何产物**（R11 失效≠删除） | preview: 409 `precondition_failed`(已 retired)；apply: `preview_stale`/`revision_conflict` |
| F8 | POST `/fragments/{fid}/boundary` → preview；`/boundary/apply` | preview:`{target_fragment_id, new_range, command_id}`；apply:`{preview_id, command_id, expected_revision, expected_range_set_revision}` | apply 200：新 FragmentRevision(reason=boundary)，ID 不变，revision+1 | preview 阶段即校验：`range_invalid`/`range_overlap`/版本非 active；apply 阶段 `preview_stale` |
| F9 | POST `/fragments/{fid}/split` → preview；`/split/apply` | preview:`{target_fragment_id, split_point, left_name?, right_name?, command_id}`；apply:`{preview_id, command_id, expected_revision, expected_range_set_revision}` | apply 200 `{left:{object_ref,range}, right:{object_ref,range}, original:{object_ref,state:'retired'}}`；原片段 retired，两个**新 ID** 各建 revision(reason=split, predecessor=[原ID])，初始 candidate | `range_invalid`（split_point 越界/劈代理对/某半空或全空白）；`precondition_failed`(非 active/已 retired)；apply `preview_stale` |
| F10 | POST `/fragments/merge` → preview；`/merge/apply` | preview:`{fragment_ids:[a,b], merged_name?, command_id}`；apply:`{preview_id, command_id, expected_revision_a, expected_revision_b, expected_range_set_revision}` | apply 200 `{merged:{object_ref,range,name}, predecessors:[a,b retired]}`；一个**新 ID**(reason=merge, predecessor=[a,b])，a/b retired，初始 candidate | 校验：恰两个、同 revision、未退役、**边界相接**（不满足 `precondition_failed` reason=`not_contiguous`）；合并范围再走重叠校验 |
| F11 | POST `/production-scopes` | `fragment_ids?\|null(=全部confirmed), command_id` | 201 范围 DTO（§1 字段）；全部列名片段须 confirmed 且 active | 422 `precondition_failed`(含非确认/非 active) |
| F12 | GET `/production-scopes?cursor=` / `GET /production-scopes/current` | — | 200 历史倒序 / 当前范围 | 404 无 |

### preview 的 impact 计算（F7–F10 公共）

- `affected[]`：以目标片段（或其后继组）为来源依据的 SourceRelation/ScriptObject/Shot/AssetBinding（附录 06 的引用字段，含引用时记录的 fragment revision）——变更只改当前版本，这些对象保留原依据。
- `needs_review[]`：冻结输入中包含该片段、且状态为 queued/running 的 StudioJob（继续用冻结输入跑完，结果留在原版本记录；不自动采用）。
- `preservable[]`：由该片段（任何 revision）输入生成/采用的 MediaArtifact、EditInstance、Release 及其实测依据。
- 无法确定映射的影响（如 F8 后旧文本位置引用）一律进 `needs_review[]`，**不承诺“只影响当前片段”**（R11）。

## 3. 并发与一致性（DB-02 验收要点）

- **集合 CAS 写入点**：F1/F8/F9/F10 的 apply（以及建集合本身）是 `range_set.cas_revision` 的唯一写入者；F4–F7/F11 不动 cas（改名/摘要/确认/退役/范围确认不改范围布局）。退役后片段不再参与重叠校验（“只退役有效范围”）。
- 同 `command_id` 重放返回首次结果；两命令同 cas 同范围：至多一个成功，另一 `revision_conflict`，**无半次保存**（fixture 见领域契约 §8）。
- 拆合后旧引用（关系/任务/媒体）继续指向被退役的原 ID；新实体的 predecessor 链可追溯；**旧引用不自动迁移**（R11/契约 §3.2）。
- 正文版本切换（附录 01 S4/S5）后：旧版本片段 DTO state 派生为 pending_review，且一切写命令 `precondition_failed`；重新激活旧版本即恢复可写，无需任何回写命令。
