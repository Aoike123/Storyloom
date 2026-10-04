# 附录 07 · 变更预览与审核决定（DB-07）

版本：2.1 · ROOT-02 冻结 · 2026-10-04。覆盖：`ChangePreview`、`ReviewDecision` 与预览公共命令。预览-应用机制的定义见[附录 00 §5](00-object-ref-and-protocol.md)，本附录落实其表结构与命令。

实施位置：表 `backend/studio/reviews/models.py`；策略 `backend/studio/reviews/impact.py`（各领域的 impact 计算函数）；命令 `backend/studio/reviews/{service,api}.py`；测试 `tests/studio/test_review_models.py`、`test_change_previews.py`。

## 1. 表

### studio_change_previews

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | preview_id |
| project_id | text(26) | NOT NULL，FK | |
| owner_id | text(26) | NOT NULL，FK→accounts.id | 发起人 |
| kind | text(20) | NOT NULL，CHECK IN ('source_activate','fragment_retire','fragment_boundary','fragment_split','fragment_merge','script_adopt','asset_version_adopt','shot_generate','asset_generate','edit_confirm','release_publish') | 冻结清单：前 9 种由 ROOT-02 各附录定义；`edit_confirm`/`release_publish` 的 payload/impact 由 ROOT-03/04 附录补充，kind 值先占位 |
| payload | text (JSON) | NOT NULL | 拟变更内容（如 new_range、target_revision、scope），按 kind 的 schema（见各附录命令定义） |
| baseline | text (JSON 数组) | NOT NULL | 冻结的 `[{kind,id,revision}]`；apply 时逐条与当前值比对 |
| impact | text (JSON) | NOT NULL | `{affected:[], needs_review:[], preservable:[]}`，每项为 `{object_ref, note?}`；`needs_review` 非空即代表“无法确定的后续影响需人工复核”（R11） |
| state | text(12) | NOT NULL，CHECK IN ('pending','applied','rejected','superseded','expired')，默认 'pending' | 终态不可逆 |
| decision_id | text(26) | NULL，FK→studio_review_decisions.id | apply 成功后回填 |
| created_at / resolved_at | timestamptz | NOT NULL / NULL | |
| expires_at | timestamptz | NOT NULL | created_at + 30 分钟 |

- 唯一约束无（预览是过程记录，可多条并存）；索引 `(project_id, state)`、`(project_id, kind)`。
- 状态迁移（只允许这些）：`pending → applied|rejected|superseded|expired`。`superseded`：任何改变 baseline 覆盖范围的写入命中时由服务置位；`expired`：读取/写入时发现 pending 且 now>expires_at 时惰性置位。**preview 不是隐式提交**：创建预览零副作用。

### studio_review_decisions

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| project_id | text(26) | NOT NULL，FK | |
| owner_id | text(26) | NOT NULL，FK | 确认人 |
| preview_id | text(26) | NULL，FK→studio_change_previews.id，**UNIQUE**（可空） | 一个预览至多一个终局决定 |
| target_ref | text (JSON ObjectRef) | NOT NULL | 被确认/拒绝的对象 |
| decision | text(10) | NOT NULL，CHECK IN ('applied','rejected') | |
| baseline_digest | text(64) | NOT NULL | sha256(该预览 baseline 的规范化 JSON) |
| created_at | timestamptz | NOT NULL | |

- **旧审核不能给新 revision 自动过关**（DB-07 验收）：决定绑定 `baseline_digest`；新内容必然产生新预览→新 digest→新决定。查询“某对象某 revision 是否已确认” = 找 target_ref+revision 匹配且 digest 匹配的行，不做模糊继承。
- 资产版本审核（AS7）同样落一行 ReviewDecision（preview_id=NULL，target_ref=asset revision，decision=confirmed/rejected 映射）——“作者确认”全库统一可查。

## 2. 公共命令（`/api/studio/projects/{pid}/previews/...`）

| # | 方法 路径 | 请求 | 成功 | 失败 |
| --- | --- | --- | --- | --- |
| RV1 | POST `/{kind 域前缀}/preview`（各附录已定义，如 `/fragments/{fid}/retire`） | 各附录的 preview 请求 | 201 预览 DTO：`{object_ref, kind, payload, baseline, impact, state, expires_at}`；同 `command_id` 重放返回原预览 | 各附录列出的校验错误 |
| RV2 | GET `/previews/{previewId}` | — | 200（惰性过期判定生效） | 404 |
| RV3 | POST `/previews/{previewId}/reject` | `command_id` | 200：state=rejected，写 ReviewDecision(decision=rejected)；**无任何业务副作用** | 409 `precondition_failed`（已终态） |
| RV4 | GET `/previews?state=&kind=&cursor=` | — | 200 列表（倒序） | 404 |
| RV5 | POST `/{各域}/apply`（各附录已定义） | 各附录的 apply 请求 | 各附录定义；统一在 apply 事务末尾写 ReviewDecision(decision=applied) 并回填 preview.decision_id | `preview_stale`（列出全部失配项，预览置 superseded）/ 各域 CAS 错误 |

## 3. 一致性（DB-07 验收要点）

- baseline 冻结发生在 preview 事务内；apply 在同一事务重读比对，**比对基准与预览时刻一致**，中间任何相关写入都会使 apply 失败并精确指出失配（fixture：领域契约 §8 的两命令争同范围、慢响应不混入他项目）。
- 每个 apply 有且仅有一个 ReviewDecision；拒绝与取消也留痕（可审计“谁在何时拒绝了什么、针对哪个基准”）。
- impact 三分类来自各领域 `impact.py` 策略函数（每个 kind 一个函数，由对应领域服务注入）；无法归类的后续影响必须进 `needs_review`，不得丢弃、不得承诺“只影响当前片段”。
- 预览/决定表不承载任何业务字段副本；payload 只存变更参数本身。
