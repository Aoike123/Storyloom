# 附录 06 · 来源关系、资产出现与生成绑定（DB-06）

版本：2.1 · ROOT-02 冻结 · 2026-10-04。覆盖：`SourceRelation`、`AssetOccurrence`、`AssetBinding` 三类**互相独立**的关系表（R05/R07：出处、出现、生成输入是不同关系）与 `/relations`、`/bindings` 命令族。共享约定见[附录 00](00-object-ref-and-protocol.md)。

实施位置：表 `backend/studio/relations/models.py`；命令 `backend/studio/relations/{service,api}.py`；测试 `tests/studio/test_relation_models.py`、`test_relations.py`。

## 1. 表

三类关系都只存引用，**不复制对象、不承载归属、不存使用计数**（使用位置即这些关系行本身，可查询）。

### studio_source_relations（出处关系）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK | |
| source_fragment_id | text(26) | NOT NULL，FK→studio_fragments.id | 出处片段 |
| source_fragment_revision | int | NOT NULL | 记录时该片段的 revision（边界变更后的历史依据不被改写，R11） |
| target_kind | text(16) | NOT NULL，CHECK IN ('script_object','shot') | |
| target_id | text(26) | NOT NULL | 按 kind 解析所属表 |
| target_revision | int | NOT NULL | 记录时目标对象的 revision |
| created_at | timestamptz | NOT NULL | |

- 唯一 `(source_fragment_id, target_kind, target_id)`。
- 与**隐式出处**的分工：script_object 的归属片段（`fragment_id` 列）、shot 的依据片段（`script_adoption_id→fragment`）是主出处，查询时标记 `primary:true`，无需关系行；本表只存**额外显式链接**（同一对象引用另一片段，如改编取材）。R05 的“整块高亮→多个对象”连线 = 主出处 + 显式链接的并集，按所选片段/对象局部展开（不累积全项目关系网）。

### studio_asset_occurrences（原文出现）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK | |
| asset_id | text(26) | NOT NULL，FK→studio_assets.id | |
| asset_revision | int | NOT NULL | 记录时的版本 |
| source_fragment_id | text(26) | NOT NULL，FK | 在原文哪个片段出现 |
| source_fragment_revision | int | NOT NULL | |
| range_start / range_end | int | NULL（成对可空） | 片段内精确范围（可选） |
| created_at | timestamptz | NOT NULL | |

- 唯一 `(asset_id, source_fragment_id, range_start, range_end)`（NULL 范围视为同一“无精确范围”桶）。
- 一个资产可在多个片段出现 → 多行，**共享同一 asset 身份**；出现是出处，不创建资产副本、不创建新片段、不承担归属（R05）。
- 双向定位：由 occurrence 回到原文片段上下文（REL-03-a）；同资产多个出处均返回原上下文。

### studio_asset_bindings（生成输入绑定）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK | |
| shot_id | text(26) | NOT NULL，FK→studio_shots.id | |
| asset_id | text(26) | NOT NULL，FK | |
| asset_revision | int | NOT NULL | **固定具体版本**；资产新版本不自动改绑定 |
| purpose | text(12) | NOT NULL，CHECK IN ('visual','background','prop') | 用途：visual=角色外观、background=物理场景、prop=道具 |
| target_character_id | text(26) | NULL，FK→studio_assets.id | 作用对象（仅 purpose=visual 且资产 type=costume 时必填，指向该服装所属人物） |
| created_at | timestamptz | NOT NULL | |

- 唯一 `(shot_id, asset_id, purpose, target_character_id)`。
- **双服装强校验**（R07）：同 shot 内 purpose=visual 且 target_character_id 相同且资产 type=costume 的绑定最多一条 → 违反返回 `precondition_failed` reason=`double_costume`（附已有 binding 的 ObjectRef）。
- 镜头生成输入 = `shot.script_adoption_id` + 该 shot 全部 binding；任务提交时整体冻结进 StudioJob（ROOT-04）。删除/修改 binding 不影响已提交任务。

## 2. 命令（`/api/studio/projects/{pid}/...`）

| # | 方法 路径 | 请求（关键字段） | 成功 | 失败（code） |
| --- | --- | --- | --- | --- |
| RL1 | POST `/relations` | `source_fragment_id, target_kind, target_id, command_id` | 201（revision 字段由服务端按当前值填写） | 422 对象不存在/跨项目；409 唯一 |
| RL2 | GET `/relations?source_fragment_id=&target_kind=&cursor=` | — | 200 关系列表（含 primary 主出处合并结果，`primary` 标记） | 404 |
| RL3 | DELETE `/relations/{relationId}` | `command_id` | 200 删除显式关系（主出处不可删；被删关系若已被冻结进任务输入，任务不受影响） | 409 `precondition_failed`（主出处） |
| OC1 | POST `/relations/occurrences` | `asset_id, source_fragment_id, range?\|null, command_id` | 201 | 422 跨项目/片段非 active；409 唯一 |
| OC2 | GET `/relations/occurrences?asset_id=&source_fragment_id=&cursor=` | — | 200（双向定位的查询基础） | 404 |
| OC3 | DELETE `/relations/occurrences/{occurrenceId}` | `command_id` | 200（无 preview：出现是标注，不承载生成输入） | 404 |
| BD1 | POST `/bindings` | `shot_id, asset_id, asset_revision?\|null(=当前), purpose, target_character_id?\|null, command_id` | 201 | 422 `precondition_failed`（revision 不存在/资产 type 与 purpose 不匹配/double_costume/非本项目）；409 唯一 |
| BD2 | GET `/bindings?shot_id=&asset_id=&cursor=` | — | 200 | 404 |
| BD3 | DELETE `/bindings/{bindingId}` | `command_id` | 200（无 preview；见上注） | 404 |
| BD4 | POST `/bindings/{bindingId}/revision` | `asset_revision, command_id` | 200：单条绑定换版本（跨镜头批量换版本走 AS9 的 preview 流程） | 422 版本不存在/不匹配；409 并发换版本 CAS（expected 由请求 `expected_asset_revision` 提供） |

- 以上写命令均幂等（附录 00 §3）；删除类命令重复执行第二次返回 404（不视为失败重试场景，客户端以 404 收敛）。
- 所有关系/绑定读取只返回本项目的行；跨项目引用在任何方向都 `not_found`。

## 3. 一致性（DB-06 验收要点）

- 三类关系分别可查询、互不推导：出现≠归属≠生成输入（DB-06 验收逐条对应）。
- 重复出现关联同一 asset 身份；同一 (fragment, asset) 的多次出现不产生新资产、新片段或第二套归属树（R05/R06）。
- binding 的版本快照语义：资产版本演进、资产当前指针切换都不改写任何 binding 行；换版本只有 BD4/AS9 两个入口且都留痕（created_at/revision 列更新为原子替换行内容并记 updated_at——**binding 行可更新版本列**，它是引用不是内容快照，更新走 CAS）。
