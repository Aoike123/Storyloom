# 附录 03 · 剧本对象、版本与采用（DB-03）

版本：2.1 · ROOT-02 冻结 · 2026-10-04。覆盖：`ScriptObject`、`ScriptRevision`、`ScriptAdoption` 与 `/script-objects`、片段级采用命令。共享约定见[附录 00](00-object-ref-and-protocol.md)。

实施位置：表 `backend/studio/scripts/models.py`；命令 `backend/studio/scripts/{service,api}.py`；测试 `tests/studio/test_scripts.py`。

## 1. 表

### studio_script_objects

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID，稳定 | |
| project_id | text(26) | NOT NULL，FK | |
| fragment_id | text(26) | NOT NULL，FK→studio_fragments.id | 唯一真实归属：一个片段 |
| kind | text(10) | NOT NULL，CHECK IN ('action','dialogue') | 动作/对白 |
| seq | int | NOT NULL | 片段内正式顺序（创建序），唯一 `(fragment_id, seq)`；坐标不参与 |
| speaker | text(80) | NULL | 对白角色名（自由标签；与资产的关联走绑定，不在此建 FK） |
| current_revision_id | text(26) | NOT NULL，FK→studio_script_revisions.id | |
| created_at / updated_at | timestamptz | NOT NULL | |
| retired_at | timestamptz | NULL | 仅当对象曾被某次采用引用后才允许退役（见 SC5） |

- 无 stage/状态列：**草稿与采用是两个事实**——最新 revision 是工作草稿，`ScriptAdoption` 钉住被采用的 revision；对象本身不因此换 ID。

### studio_script_revisions（不可变）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| object_id | text(26) | NOT NULL，FK | 唯一 `(object_id, revision)` |
| revision | int | NOT NULL，自 1 起 | |
| text | text | NOT NULL | 动作/对白正文 |
| speaker | text(80) | NULL | 随内容冻结（修订可换 speaker） |
| adaptation_note | text(300) | NULL | 改编说明（R07“出处与改编内容”） |
| source_fragment_revision | int | NOT NULL | 建立该对象时所依据的片段 revision（出处快照；片段边界变更后此处不变） |
| created_at | timestamptz | NOT NULL | |

### studio_script_adoptions（片段级采用绑定）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK | |
| fragment_id | text(26) | NOT NULL，FK | |
| object_revisions | text (JSON 数组 `[{object_id, revision, seq, kind}]`) | NOT NULL | 采用时刻该片段全部对象的 revision 快照 |
| content_hash | text(64) | NOT NULL | sha256(按 seq 排序拼接 `revision:sha256(text)`) 的指纹，稳定可比 |
| previous_adoption_id | text(26) | NULL，FK→本表.id | 采用链（首个为 NULL） |
| created_at | timestamptz | NOT NULL | |

- 片段的当前采用 = 链尾（`previous_adoption_id IS NULL` 反查或按 created_at 取该 fragment 最新行）。
- 采用不可变；“编辑草稿 ≠ 采用”（DB-03 验收）。镜头输入引用具体 adoption（附录 04），旧采用不被新采用改写。

## 2. 命令（`/api/studio/projects/{pid}/...`）

| # | 方法 路径 | 请求（关键字段） | 成功 | 失败（code） |
| --- | --- | --- | --- | --- |
| SC1 | POST `/script-objects` | `fragment_id, kind, text, speaker?, adaptation_note?, seq?\|null(=下一位), command_id` | 201 DTO（revision=1）。片段须 confirmed 且绑定 active 正文版本 | 422 `precondition_failed`(片段非 confirmed/非 active/已 retired)、`validation_failed`(text 空)；409 seq 占用 |
| SC2 | GET `/script-objects?fragment_id=&cursor=` | — | 200 列表 DTO | 404 |
| SC3 | GET `/script-objects/{oid}` | — | 200 DTO：object_ref、kind、text、speaker、adaptation_note、seq、fragment(object_ref)、current_revision、`adopted`(当前 revision 是否被片段当前采用钉住)、adopted_in(adoption ref\|null) | 404 |
| SC4 | POST `/script-objects/{oid}/revise` | `text?, speaker?, adaptation_note?, expected_revision, command_id` | 200 DTO：新 revision（=当前草稿+1）；**不触碰采用**，不启动任何生成 | 409 `revision_conflict`（过期保存拒绝）；422 字段校验/片段非 active |
| SC5 | POST `/script-objects/{oid}/remove` | `expected_revision, command_id` | 200：对象及其全部 revision 删除（仅当未被任何采用引用） | 409 `precondition_failed` reason=`object_adopted`（提示：先重新采用一组不含它的草稿）；422 `revision_conflict` |
| SC6 | POST `/fragments/{fid}/script-adopt` → preview；`/script-adopt/apply` | preview:`{target_fragment_id, command_id}`；apply:`{preview_id, command_id, expected_previous_adoption_id\|null, expected_object_revisions(同 SC1 快照格式)}` | apply 200：新 adoption（链尾）；**不自动生成分镜**，不改变任何已冻结镜头输入 | preview:422 片段无对象/非 confirmed；apply:`preview_stale`/`revision_conflict` |
| SC7 | GET `/fragments/{fid}/script-adoptions?cursor=` | — | 200 采用历史倒序 | 404 |

### SC6 的 impact（附录 00 §5）

- `baseline`：`[{script_object, id, revision} × 片段全部对象]` + 当前 adoption id。
- `affected[]`：引用该片段/该片段旧采用的镜头（其输入冻结不受本次采用影响——列出仅为提示）。
- `needs_review[]`：冻结输入引用旧采用且 queued/running 的 StudioJob。
- `preservable[]`：以旧采用为剧本依据生成的视频及其媒体/发布依据。

## 3. 一致性（DB-03 验收要点）

- 同片段至少两个不同对象可并存；`(fragment_id, seq)` 唯一；坐标/画布位置不进入任何列。
- 版本行不可变；草稿编辑 = 追加 revision；采用 = 钉住 revision 快照；**版本更新不复制业务归属**（fragment_id 只在对象行上）。
- 采用链可回溯任意历史剧本；旧采用上的镜头输入在后续采用后保持不变（R11）。
