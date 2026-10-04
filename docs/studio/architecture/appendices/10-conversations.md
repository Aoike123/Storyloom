# 附录 10 · 会话与智能提案（ROOT-04）

版本：2.2 · ROOT-04 冻结 · 2026-10-04。覆盖：会话/消息/提案表（DB-09）、会话作用域冻结、提案白名单、响应不直写业务。依据：[产品规范 R04/R06](../../product/product-spec.md)、[领域契约 §2](../domain-contracts.md)、附录 09（供应商 LLM 适配器）。

实施位置：`backend/studio/conversations/`（models/schemas/service/api）；前端 `web/features/studio/chat/`。

## 1. 表（DB-09）

```text
studio_conversations
  id            PK ULID
  project_id    FK studio_projects
  owner_id      FK users
  scope_kind    TEXT   -- project | fragment | scene | shot | asset | edit
  scope_id      ULID   -- 该 scope 的对象 id（project 时 = project_id）
  title         TEXT
  created_at / updated_at
  UNIQUE (project_id, scope_kind, scope_id, owner_id)   -- 每作用域每人一个会话

studio_messages
  id            PK ULID
  conversation_id FK studio_conversations
  sender        TEXT   -- user | assistant
  content       TEXT   -- 纯文本（渲染为对话气泡）
  refs          JSON nullable    -- [{kind,id,label}] 渲染为引用芯片
  job_id        ULID nullable    -- 关联任务（进度可查）
  created_at

studio_proposals
  id            PK ULID
  conversation_id FK studio_conversations
  message_id    FK studio_messages   -- 产生该提案的 assistant 消息
  kind          TEXT   -- 白名单见 §3
  payload       JSON           -- 对应命令 DTO（附录 01–08/09/11）
  status        TEXT   -- pending | adopted | rejected | superseded | expired
  decision_note TEXT nullable
  created_at / decided_at
```

- 消息与提案只追加；提案 `superseded` = 同一目标对象被更新的提案取代（同 kind+同目标 ref 仅一个 pending）。

## 2. 会话作用域（冻结）

- 会话**严格限定在单一项目与单一作用域对象**；上下文包 = 该作用域对象的当前规格 + 直接关联引用（如 shot 会话含其 adoption/binding 摘要），**不跨项目、不带全库历史**。
- 上下文构建只读（走各域 service 的 detail/refs 端点），不写业务。
- 历史裁剪：LLM 请求只带最近 20 条消息 + 当前上下文包（token 预算 8K）；更早消息保留在库但不出请求。

## 3. 提案白名单（冻结；白名单外只能是纯文本建议）

| 类 | kind（= 对应命令/preview kind） |
| --- | --- |
| 来源 | `source_import`（粘贴全文导入） |
| 片段 | `fragment_rename`、`fragment_summary`、`fragment_split`（preview）、`fragment_merge`（preview）、`fragment_boundary`（preview）、`fragment_retire`（preview） |
| 剧本 | `script_adopt` |
| 镜头/场 | `scene_create`、`shot_create`、`shot_update`、`shot_reorder`（preview）、`shot_generate`（preview→job） |
| 资产 | `asset_create`、`asset_new_revision`（可触发 `asset_generate` job）、`asset_review`（确认/拒绝）、`asset_version_adopt`（preview） |
| 关系 | `binding_add`、`binding_change` |
| 剪辑/发布 | `edit_instance_add`、`edit_instance_remove`、`export`（preview→job）、`release_publish`（preview→job） |

- **提案应用 = 执行对应领域命令**：该命令若属 00 §5 必须预览清单，则提案应用只创建/打开 ChangePreview，由用户在预览页二次确认——**提案永不绕过预览、永不直写业务表**（验收）。
- payload 用附录中的命令 DTO 校验；校验失败 → 提案 `expired` + 原因，assistant 文本提示修正。
- 每个提案卡片显示：kind 标签、payload 摘要、影响列表（preview 类）、采用/拒绝按钮；采用后状态流转并回显结果（成功/预览已创建）。

## 4. 助手响应协议

- 一次 LLM 调用返回 `{text, proposals[]}`（JSON 模式，附录 09 `llm.py`）；`proposals[]` 每项 = {kind, payload}，逐条建 `studio_proposals`。
- **响应不直接写业务**（验收）：assistant 消息落库 = message（+refs）；只有用户逐条采用才产生业务命令。
- 失败：LLM 未配置/出错 → 会话页显示配置门槛或错误态（沿用 09 §4 计费边界，会话调用属收费调用，同样不自动重发）。
- 无 Gxx 状态样式、无节点名（R02/R03、验收）：进度用纯语言（“正在生成镜头 3/5，剩余约 40 秒”）。
