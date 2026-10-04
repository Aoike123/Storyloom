# 附录 00 · ObjectRef、幂等/CAS、错误协议与预览-应用协议

版本：2.1 · ROOT-02 冻结 · 2026-10-04。性质：所有新领域附录与执行器任务卡的**共享技术约定**。本文不定义任何具体业务表；具体表/命令见同目录附录 01–07 及 ROOT-03/04 产物。

依据：[领域契约 §2/§3.1](../domain-contracts.md)、[产品规范 R11](../../product/product-spec.md)。实现位置：`backend/studio/contracts/`（ObjectRef 与领域间端口）、`backend/core/`（错误序列化与鉴权，BASE-03 冻结范围）。

## 1. ObjectRef

所有跨对象引用统一为：

```json
{ "kind": "fragment", "id": "01J8ZK4M9XQ2V7R3N5T6W8Y0A1", "revision": 3 }
```

- `kind`：见下表。`id`：服务端生成的 ULID（26 位 Crockford 大写字母数字，时间有序），**客户端永不生成实体 ID**，只生成 `command_id`（UUIDv4）与选择态。
- `revision`：该对象当前语义版本的整数（见 §2 各聚合定义）；不可变对象（StoryRevision、MediaArtifact、Release、已确认的 ConfirmedEdit）的 `revision` 恒为其版本序号。
- 任何引用在使用前先校验：属于当前项目（同 `project_id` 或项目内共享资产）、版本存在、对象可用（未退役/未取代）。失败返回 `not_found` 或 `precondition_failed`，附 ObjectRef。
- 版本更新**不换实体 ID**；拆合可以产生新实体 ID 并记录 `predecessor_ids` 继承。
- 业务归属只来自各附录定义的真实外键；树视图、画布节点是查询结果，不是第二套可写树。

| kind | 对象 | 所在附录 | 备注 |
| --- | --- | --- | --- |
| `project` | StudioProject | 01 | |
| `source_revision` | StoryRevision | 01 | 不可变 |
| `range_set` | RangeSet | 02 | (project, source_revision) 唯一 |
| `fragment` | Fragment | 02 | |
| `script_object` | ScriptObject | 03 | |
| `script_adoption` | ScriptAdoption | 03 | 片段级采用绑定 |
| `scene` | Scene | 04 | |
| `shot` | Shot | 04 | |
| `asset` | Asset | 05 | 项目内共享 |
| `source_relation` | SourceRelation | 06 | 出处关系 |
| `asset_occurrence` | AssetOccurrence | 06 | 原文出现 |
| `asset_binding` | AssetBinding | 06 | 生成输入绑定 |
| `change_preview` | ChangePreview | 07 | |
| `review_decision` | ReviewDecision | 07 | |
| `job` / `job_attempt` / `job_event` | StudioJob 族 | [附录 09](09-jobs-and-providers.md) 冻结 | 本附录只登记 kind |
| `conversation` / `message` / `proposal` | 会话族 | [附录 10](10-conversations.md) 冻结 | 同上 |
| `edit` / `edit_instance` / `confirmed_edit` | 剪辑族 | [附录 08/11](11-releases.md) 冻结 | 同上 |
| `media_artifact` / `release` | 媒体/发布族 | [附录 08/11](11-releases.md) 冻结 | 同上 |

## 2. ID 与版本字段总则

- 实体 ID：ULID，主键，全库唯一。表名一律 `studio_` 前缀（如 `studio_fragments`）。
- 可变聚合同时保存：`current_revision_id`（指向版本行）与版本行的自增 `revision`（从 1 起）。写入该聚合的命令携带 `expected_revision`（= 调用方所见的 `current_revision_id` 行的 `revision`），事务内 compare-and-swap，不符返回 `revision_conflict`（含 expected/actual）。
- 不可变聚合（StoryRevision、MediaArtifact、Release、ConfirmedEdit）：只追加，无更新命令；“当前”由所属聚合的外键（如 `project.active_source_revision_id`、`asset.current_revision_id`）表达，切换“当前”本身是带 CAS 的命令。
- 时间：全部存 UTC；整数毫秒用于媒体时间轴，其余用 `timestamptz`。
- 每个聚合有 `created_at`、`updated_at`；退役用状态字段 + `retired_at`，不用物理删除（本轮无任何批量清库）。

## 3. 命令幂等（command_id）

- 每个**写**命令请求体必含 `command_id`（客户端 UUIDv4，同一逻辑命令重试时不变；UI 每次新发起生成新的）。
- 表 `studio_command_records`：`id`、`owner_id`、`project_id`(可空)、`command_id`、`result_payload`(JSON，首次成功的完整响应体)、`created_at`；唯一约束 `(owner_id, command_id)`。
- 同 `command_id` 重放：不执行副作用，返回首次的 `result_payload`（HTTP 200）。`command_id` 不属于任何业务对象，只保证“不重复执行”。
- 两命令以同一 `expected_revision` 争同一资源：事务串行化后**至多一个成功**，另一收到 `revision_conflict` 且无半次保存（所有副作用在同一事务内）。
- 读请求不接受 `command_id`，也不得产生任何副作用（不建对象、不迁移记录）。

## 4. 错误协议

统一响应体（所有非 2xx）：

```json
{ "error": { "code": "range_overlap", "message": "该范围与已有片段重叠", "details": { } } }
```

冻结错误码（新增需升 ROOT-02 版本并登记受影响任务）：

| code | HTTP | 含义 | details 必填字段 |
| --- | --- | --- | --- |
| `unauthenticated` | 401 | 无有效会话 | — |
| `forbidden` | 403 | 跨属主/无权限（含公开端点取私有字段） | `{"kind","id"}` |
| `not_found` | 404 | 对象不存在或不属于当前项目 | `{"kind","id"}` |
| `validation_failed` | 422 | 请求字段/业务校验失败 | `{"violations":[{"field","rule","message"}]}` |
| `range_invalid` | 422 | 范围本身非法 | `{"rule"}`，`rule ∈ empty｜blank｜out_of_bounds｜non_integer｜proxy_split｜non_continuous` |
| `range_overlap` | 409 | 与已保存候选/确认重叠（含重复/相交/包含） | `{"conflicts":[{"fragment_id","name","start","end"}]}` |
| `revision_conflict` | 409 | CAS 失败 | `{"object":{"kind","id"},"expected","actual"}` |
| `preview_stale` | 409 | 预览基准已变 | `{"preview_id"}` + 与 `revision_conflict` 同形的 `conflicts[]` 列表 |
| `precondition_failed` | 422 | 状态前置不满足（如未确认片段不可采用、同镜头同人双服装） | `{"reason","blocked_by":{"kind","id","revision"}}` |
| `duplicate_scope` | 422 | 完整制作范围重复提交同一覆盖 | `{"scope_ref"}` |
| `duplicate` | 409 | 同一作用域内同身份对象已存在（v2.6 增：项目名 (owner,name) 等属主级 UQ 冲突；不改已有对象） | `{"scope_ref"}` |
| `internal_error` | 500 | 未预期错误；不泄露内部细节 | — |

- `message` 为面向用户的准确说明；`details` 供程序恢复。
- **v2.6 语义区分**：`duplicate`（409）= 属主级身份唯一冲突（如 P1 项目名）；`duplicate_scope`（422）= 覆盖范围重复提交（制作范围、发布创建等附录 11 §3 场景）。两者不互换。前端客户端（BASE-02）必须保留 code 与 details，失败不得转成成功空数组。
- 409/422 的 message 必须给出**具体已有对象与定位信息**（ID + 名称），不静默裁掉或偷合并。

## 5. 预览-应用协议（R11 的统一机制）

所有“影响他人”的变更走两步，统一由 `ChangePreview`（附录 07）承载：

1. **preview**：`POST <领域>/preview`，请求含目标对象、变更内容与 `command_id`（preview 本身是一次幂等写，生成 preview 记录）。服务端解析并冻结：
   - `baseline`：被冻结的各 ObjectRef 及其 `revision`（如目标片段当前 revision、所依赖的 range_set cas、相关 script_adoption/shot revision 摘要）；
   - `impact`：三类清单 —— `affected[]`（直接受影响对象）、`needs_review[]`（无法确定影响、需人工复核，**不得承诺“只影响当前片段”**）、`preservable[]`（保留的历史产物及其依据）；
   - 返回 `{preview_id, kind, baseline, impact}`，状态 `pending`。
2. **apply**：`POST <领域>/apply`，请求含 `preview_id` + `command_id`（新的 UUID）+ 该领域要求的 `expected_*` 字段。服务端在同一事务内逐条比对 baseline 与当前值：不符 → `preview_stale`（列出全部失配项，预览置 `superseded`）；相符 → 执行变更、预览置 `applied`、写 `ReviewDecision`（decision=`applied`）。
3. 用户可显式取消：`POST /api/studio/projects/{pid}/previews/{previewId}/reject` → 预览置 `rejected`，无任何副作用。
4. 预览 TTL 30 分钟；到期自动 `expired`。任何改变 baseline 覆盖范围的写入使该预览 `superseded`。
5. **哪些变更必须走 preview**（冻结清单）：来源正文更新（含激活切换）、片段边界变更、拆分、合并、片段退役、采用剧本版本、采用资产新版本（跨场/跨镜头范围）、镜头输入冻结、发布/导出前的最终确认。**哪些不需要**：新建候选片段、改名、新建剧本对象/草稿、创建场/镜头、新建资产、绑定/出现/出处关系、纯读、纯草稿编辑、任务命令本身（其恢复语义见 ROOT-04）。不需要 preview 的命令仍执行 CAS 与幂等。
6. “采用/审核/确认/发布”是不同动作：preview 不提交，apply 是明确的作者确认，审核（job `needs_review`）与采用分别记录（ROOT-04）。

## 6. 查询 DTO

- 单对象读：`200`，响应体 = 该附录定义的**完整对象 DTO**（含 `object_ref`、真实父级、来源、版本、状态与可编辑字段）。DTO 字段名与表字段一致，camelCase 序列化。
- 列表读：`200 {"items": [...], "next_cursor": "<ULID|null>"}`；游标 = 上一页最后一条 ID（ULID 有序），`?cursor=` 续读，默认页大小 50，上限 200。空列表返回 `{"items":[],"next_cursor":null}`。
- 公开端点 `/api/studio/public/*`：只返回白名单字段（附录 01 定义），不返回原文正文、过程资产、私有对话；未公开内容对任何账号（含属主之外）表现为 `not_found`。
- 鉴权：每个 HTTP 读写都校验会话与属主（BASE-03-c）；`/api/studio/public/*` 例外，仅白名单。

## 7. 范围校验公共规则（附录 02 的执行依据）

- 范围 = UTF-16 code unit 半开区间 `[start, end)`，相对 canonical 文本（`offset_policy="lf-utf16-v1"`）。
- 拒绝（`range_invalid`）：非整数/缺失、`end<=start`（空）、越界（`end>len` 或 `start<0`）、区间内全空白（blank）、劈开代理对（区间边界落在代理对内侧）、非连续选择（前台选区必须在提交前合成单一连续区间）。
- **blank 空白集（v2.5 冻结）**：区间内每个码点均属此集才判 blank = **JS `\s` 码点集**：`U+0009~U+000D`、`U+0020`、`U+00A0`、`U+1680`、`U+2000~U+200A`、`U+2028`、`U+2029`、`U+202F`、`U+205F`、`U+3000`、`U+FEFF`。前后端显式用同一集（Python 不用 `isspace()`——其与 JS `\s` 在 U+0085/U+001C-1F/U+FEFF 等码点不一致），保证任意文本上两栈判定严格相等（SOURCE-02 验收裁定）。
- 冲突（`range_overlap`）：与**同 (project, source_revision) 下所有未退役候选/确认**逐一判 `a.start < b.end && b.start < a.end`；重复、相交、包含、被包含均冲突，边界相接合法。AI 提案与用户操作走同一服务端规则。

## 8. 前端 HTTP 客户端传输（BASE-02 的冻结依据）

- **Base URL**：默认 `http://127.0.0.1:3011`（ROOT-01 的 backend_port）；可用构建期环境变量 `NEXT_PUBLIC_STUDIO_API_BASE` 覆盖；**永不指向旧后端 8000**。后端 CORS 白名单只放行 `http://127.0.0.1:3021`（ROOT-01 的 frontend_port，BASE-01-b 已装配）。
- **鉴权**：`Authorization: Bearer <token>`；token 存 `localStorage` 键 `sl_studio_token`（登录/注册成功后由登录界面写入；登录端点属 `/api/studio/auth/*`，由 BASE-03-c 接线）。无 token 时请求照常发出，服务端 401 `unauthenticated`。
- **请求体**：各附录冻结的命令 DTO；需要幂等的命令携带 `command_id`（UUID，客户端对**每个用户动作生成一次**）；需要 CAS 的命令携带对应 `expected_*` 字段。**重试/重发同一逻辑命令必须复用原 command_id**（客户端提供“保留 id 重试”能力；服务端 CommandRecord 按 (owner_id, command_id) 去重，重复请求返回首次结果）。
- **超时**：默认 30s（AbortController）；超时/断网/HTTP 层失败 → `network` 错误（无响应体），UI 显示“连接失败，请重试”；**任何失败都不得映射为成功或空数组/空对象**。
- **前端错误类型**（与 §4 对齐）：`StudioApiError { ok:false, status?:number, code, message, details? }`，`code ∈ §4 十二码 | "network"`；`details` 原样保留（range_overlap 的 conflicts[]、revision_conflict 的 object/expected/actual 等），UI 用 message 展示、用 details 定位冲突对象。
- **成功类型**：`StudioApiResult<T> { ok:true, data:T }`；列表接口的 `data = {items, next_cursor}`（§6）。
- 客户端只被 `web/features/studio/**` 使用；旧 app 代码不 import 它，它不 import 旧代码。
