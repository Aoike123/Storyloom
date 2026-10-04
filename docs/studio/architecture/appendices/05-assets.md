# 附录 05 · 资产与并存版本（DB-05）

版本：2.1 · ROOT-02 冻结 · 2026-10-04。覆盖：`Asset`、`AssetRevision` 与资产命令族（规格/媒体/审核/采用）。共享约定见[附录 00](00-object-ref-and-protocol.md)。

实施位置：表 `backend/studio/assets/models.py`；命令 `backend/studio/assets/{service,api}.py`；测试 `tests/studio/test_asset_models.py`、`test_assets.py`。

## 1. 表

### studio_assets

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID，稳定 | 项目内共用：同一 ID 被多片段/多场引用，**不按片段复制** |
| project_id | text(26) | NOT NULL，FK | |
| type | text(10) | NOT NULL，CHECK IN ('character','costume','scene') | 人物身份/服装/物理场景（R07） |
| name | text(120) | NOT NULL | 就地改名不改身份 |
| character_id | text(26) | NULL，FK→本表.id | type=costume 时必填且目标 type 必须 character；character/scene 必须 NULL |
| costume_required | bool | NOT NULL，默认 true | 仅 character 有意义：可**明确**置 false（自然体表角色），不用空图冒充服装 |
| current_revision_id | text(26) | NOT NULL，FK→studio_asset_revisions.id | “当前版本”指针；新 revision 自动成为当前，可用 AS8 切回旧版本 |
| created_at / updated_at | timestamptz | NOT NULL | |

### studio_asset_revisions（不可变，并存）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | 唯一 `(asset_id, revision)` |
| asset_id | text(26) | NOT NULL，FK | |
| revision | int | NOT NULL，自 1 起 | |
| spec | text (JSON) | NOT NULL | 按 type 的必填键（见下）；自由扩展键允许 |
| media_artifact_id | text(26) | NULL，FK→studio_media_artifacts.id（ROOT-03/04 表） | 该版本的真实媒体（上传或生成）；NULL=尚无媒体 |
| generation_input | text (JSON) | NULL | 实际生成输入快照（提示词/参数/模型；供应商细节见 ROOT-04）；上传版本为 NULL |
| review_status | text(12) | NOT NULL，CHECK IN ('unreviewed','confirmed','rejected')，默认 'unreviewed' | **作者确认**这一个事实；结构校验/AI 预审/任务完成是其他机制的状态，不混入本列（R07） |
| created_at | timestamptz | NOT NULL | |

`spec` 必填键（冻结）：

| type | 必填键 | 可选键 |
| --- | --- | --- |
| character | `appearance`(string) | `body_type`('natural'\|'clothed')、`height_cm`、`notes` |
| costume | `description`(string) | `colors`[string]、`occasion` |
| scene | `location`(string)、`description`(string) | `time_of_day`、`weather` |

- 生成/上传形成新版本（revision+1）；**确认新版本不让任何场/镜头自动采用**——镜头引用的是 binding 固定的版本（附录 06），已发布快照不变（R07）。

## 2. 命令（`/api/studio/projects/{pid}/...`）

| # | 方法 路径 | 请求（关键字段） | 成功 | 失败（code） |
| --- | --- | --- | --- | --- |
| AS1 | POST `/assets` | `type, name, character_id?(costume 必填), spec, command_id` | 201 DTO（revision=1，unreviewed，无媒体） | 422 `precondition_failed`(costume 无 character/character 带 character_id)、`validation_failed`(spec 缺必填键) |
| AS2 | GET `/assets?type=&cursor=` / GET `/assets/{aid}` | — | 200 列表 / 详情：object_ref、type、name、character(ref)、current_revision（含 spec/media/ref/`review_status`）、`versions[]`（并存版本倒序）、`occurrences[]`、`bindings[]`（附录 06，含位置上下文） | 404 |
| AS3 | POST `/assets/{aid}/rename` | `name, command_id` | 200 | 422 |
| AS4 | POST `/assets/{aid}/spec` | `spec, expected_revision, command_id` | 200：新 revision，**继承上一 revision 的 media_artifact_id**（同媒体、新规格），review_status 回到 unreviewed，自动成为当前 | 409 `revision_conflict`；422 spec 键 |
| AS5 | POST `/assets/{aid}/media` | 文件上传 + `expected_revision, command_id` | 200：新 revision 带真实 MediaArtifact（私有存储，ROOT-03），review_status=unreviewed，自动当前 | 409；422 格式/大小（ROOT-03 冻结的媒体限制） |
| AS6 | POST `/assets/{aid}/generate` → preview；`/generate/apply` | preview:`{target_asset_id, spec?, command_id}`；apply:`{preview_id, command_id, expected_revision}` | preview 201（kind=`asset_generate`，成本见 ROOT-04）；apply → StudioJob（ROOT-04）产出新 revision | preview:422 `precondition_failed`(spec 不完整)；apply:`preview_stale`/ROOT-04 任务错误 |
| AS7 | POST `/assets/{aid}/revisions/{revId}/review` | `decision('confirmed'\|'rejected'), command_id` | 200：该 revision 的 review_status 更新；**主按钮即明确确认**，不要求额外勾选（R08）；不改 current 指针、不改任何 binding | 422 `precondition_failed`(revision 属他人/不存在) |
| AS8 | POST `/assets/{aid}/current` | `target_revision, expected_revision, command_id` | 200：current 指针切到指定并存版本；binding/采用不受影响 | 409 `revision_conflict` |
| AS9 | POST `/assets/{aid}/adopt` → preview；`/adopt/apply` | preview:`{target_asset_id, target_revision, scope:{shots:[shot_id...]\|'all_bound'}, command_id}`；apply:`{preview_id, command_id, expected_revision, expected_binding_digest}` | apply 200：范围内各镜头的对应 binding 更新到 target_revision（其余 binding 不动） | preview:422 范围解析失败/目标版本不存在；apply:`preview_stale`/`revision_conflict` |
| AS10 | POST `/assets/{aid}/costume-required` | `value(bool), expected_revision, command_id` | 200：character 的 costume_required 更新 | 422 非 character；409 |

### AS9 的 impact（跨场/跨镜头采用，必须 preview）

- `baseline`：`{asset revision, binding 集合 digest, scope 内各 shot id+revision}`。
- `affected[]`：scope 内每个 binding（shot、scene、旧版本→新版本）。
- `needs_review[]`：冻结输入引用受影响 binding 且在途的 job（继续用旧版本跑完）。
- `preservable[]`：以旧版本为输入已生成的视频/发布依据。

## 3. 一致性（DB-05 验收要点）

- 同资产新版本不换 ID、不新建身份；并存的版本行全部可查（详情展示并存版本，R07）。
- 服装→人物 FK 与 costume_required 构成人物/服装关系一致性；同一镜头同一人物不能绑两套服装（在附录 06 的 binding 上强校验）。
- 项目共用：资产不属任何片段副本；出现/使用都是关系（附录 06），不另存可写计数。
