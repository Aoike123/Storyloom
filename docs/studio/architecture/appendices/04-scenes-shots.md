# 附录 04 · 场、镜头与镜头版本（DB-04）

版本：2.1 · ROOT-02 冻结 · 2026-10-04。覆盖：`Scene`、`Shot`、`ShotRevision` 与场/镜头命令族、显式重排、生成入口（生成任务本体由 ROOT-04 冻结）。共享约定见[附录 00](00-object-ref-and-protocol.md)。

实施位置：表 `backend/studio/storyboard/models.py`；命令 `backend/studio/storyboard/{service,api}.py`；测试 `tests/studio/test_shot_models.py`、`test_storyboard.py`。

## 1. 表

### studio_scenes

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK | |
| name | text(120) | NOT NULL | 就地改名，无版本 |
| seq | int | NOT NULL | 场正式顺序，唯一 `(project_id, seq)` |
| layout_cas_revision | int | NOT NULL，默认 1 | 镜头重排的唯一 CAS 写入点（对标 range_set） |
| created_at / updated_at | timestamptz | NOT NULL | |

### studio_shots

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID，稳定 | |
| project_id | text(26) | NOT NULL，FK | |
| scene_id | text(26) | NOT NULL，FK→studio_scenes.id | **唯一父级**：镜头属于且只属于一场；无第二归属 |
| seq | int | NOT NULL | 场内正式顺序，唯一 `(scene_id, seq)`；**坐标不推断顺序** |
| name | text(120) | NOT NULL | 就地改名 |
| script_adoption_id | text(26) | NOT NULL，FK→studio_script_adoptions.id | 该镜头依据的采用剧本（固定到具体 adoption）；出处=其片段 |
| current_revision_id | text(26) | NOT NULL，FK→studio_shot_revisions.id | |
| created_at / updated_at | timestamptz | NOT NULL | |

- 跨项目/跨场 FK 校验拒绝（DB-04 验收“错误外键/跨项目引用拒绝”）。
- 镜头不提供删除/退役命令（本轮范围）；未采用前它只是草稿容器，不影响任何下游。

### studio_shot_revisions（不可变）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | 唯一 `(shot_id, revision)` |
| shot_id | text(26) | NOT NULL，FK | |
| revision | int | NOT NULL，自 1 起 | |
| visual | text | NOT NULL | 画面描述 |
| action | text | NOT NULL | 动作 |
| dialogue | text | NULL | 对白 |
| transition_in / transition_out | text(60) | NULL | 衔接 |
| duration_hint_ms | int | NULL | 目标时长提示（实际时长由生成媒体决定，见 ROOT-03） |
| created_at | timestamptz | NOT NULL | |

- 采用剧本/资产**不存副本**：镜头输入 = `shot.script_adoption_id` + `AssetBinding`（附录 06）+ 当前 revision；生成任务提交时把这些冻结进任务（ROOT-04），此处只保证引用可解析。
- “草稿编辑”= 追加 revision；生成前检查（R07“采用剧本、必要资产、人工审核的具体版本”）在任务提交时按引用解析并逐一校验，缺失项以 `precondition_failed.blocked_by[]` 列出。

## 2. 命令（`/api/studio/projects/{pid}/...`）

| # | 方法 路径 | 请求（关键字段） | 成功 | 失败（code） |
| --- | --- | --- | --- | --- |
| SB1 | POST `/scenes` | `name, seq?\|null(=下一位), command_id` | 201 场 DTO | 422 校验/`revision` 无关；409 seq 占用 |
| SB2 | GET `/scenes?cursor=` / GET `/scenes/{sid}` | — | 200 列表（含每场镜头数）/ 详情（含镜头列表摘要） | 404 |
| SB3 | POST `/scenes/{sid}/rename` | `name, command_id` | 200 | 422 |
| SB4 | POST `/scenes/{sid}/shots` | `name, script_adoption_id, command_id` | 201 镜头 DTO（revision=1 由请求内可选字段或空草稿起步）。adoption 必须属本项目、其片段 confirmed | 422 `precondition_failed`(adoption 不存在/跨项目/片段状态) |
| SB5 | POST `/shots/{shotId}/revise` | `visual?, action?, dialogue?, transition_in?, transition_out?, duration_hint_ms?, expected_revision, command_id` | 200 DTO：新 revision（当前草稿+1） | 409 `revision_conflict`；422 必填字段（visual/action 为空拒绝） |
| SB6 | POST `/shots/{shotId}/rename` | `name, command_id` | 200 | 422 |
| SB7 | POST `/scenes/{sid}/reorder` | `order:[shot_id...], expected_layout_cas_revision, command_id` | 200：整场镜头按给定顺序重排 seq；**显式命令**（R07：自由拖动不改顺序） | 422 `precondition_failed`(order 与当前集合不符)；409 `revision_conflict`(layout cas) |
| SB8 | GET `/shots/{shotId}` | — | 200 DTO：object_ref、scene、name、seq、current_revision 全字段、script_adoption(ref)、bindings[](附录 06)、occurrence/来源摘要、`generated_media`(job 产物 ref，ROOT-04) | 404 跨项目 |
| SB9 | POST `/shots/{shotId}/generate` → preview；`/generate/apply` | preview:`{target_shot_id, command_id}`；apply:`{preview_id, command_id, expected_revision, expected_binding_set_digest}` | preview 201（kind=`shot_generate`）：baseline=shot revision+bindings 快照+script adoption；impact 见下。apply → 提交 StudioJob（ROOT-04 的输入冻结/队列/成本） | preview:422 `precondition_failed`（缺采用剧本/必要资产未确认版本，`blocked_by[]` 列全部缺口）；apply:`preview_stale`/`revision_conflict`/ROOT-04 定义的任务错误 |

### SB9 的 impact

- `affected[]`：该镜头（新 job 指向其当前 revision）。
- `needs_review[]`：无（新 job 不动旧结果）；同一镜头的在途 job 列出以提示排队。
- `preservable[]`：该镜头已有生成视频（不受新 job 影响）。
- 生成前展示实际范围、必要输入与成本信息（R08）；成本字段由 ROOT-04 定义，未知即标未知。

## 3. 一致性（DB-04 验收要点）

- 镜头一份父级（scene_id）；顺序只在 `(scene_id, seq)` 与 `layout_cas_revision` 中表达；画布坐标不进表。
- 重排是整场原子操作：事务内一次性重算 seq，layout cas +1；并发重排至多一个成功。
- 采用剧本/资产是引用不是副本：adoption 或 binding 变化不静默改变已存在 revision 的含义；已发布/已生成依据不变（R11）。
