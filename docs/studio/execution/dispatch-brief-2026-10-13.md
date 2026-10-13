# 批次 A 派发交接单 · 2026-10-13

本文件是主模型给**下一个会话**的交接：记录已冻结的接口、已完成的准备、已落盘的部分产出
与剩余派发范围，使新会话无需重新推导即可直接派发。

当前基线：分支 `studio-rebuild`，提交见下方“已完成”小节。派发前请重新核对 HEAD 与工作树。

## 1. 为什么必须在新会话派发

`~/.dsh/profiles/web/cordis.patch.yml` 配置了子执行器模型
`subagent-model-selection-settings: { enabled: true, allowedModels: [pation/qwen38-27b-w3a4-64k] }`，
该 provider 是本地端点（`http://127.0.0.1:18982/v1`），`qwen38-27b-w3a4-64k` 上报
`max_concurrent_requests: 8`（另两档 192k=2、256k=1），即项目并行容量上限 8。

但模型选择能力**只在会话装配时采样**（`tool-subagent` 的 `modelSelectionSettings` +
会话投影 `subagent/model-selection-policy`）。2026-10-13 03:40 创建的会话没有该投影事件，
其委派工具是固定路由，`subagent` 工具不暴露 `provider`/`model` 参数；实测
`workflow` 的 `agent(..., {provider:'pation'})` 与 `{provider:'pation', model:'qwen38-27b-w3a4-64k'}`
均返回 `null`（子代理起不来），默认路由正常。

结论：要用上配置的模型，必须在**新会话**中派发；本会话无法中途切换。

## 2. 已完成（可直接作为新会话的基线）

| 提交 | 内容 |
| --- | --- |
| `880539b` | 并行派发容量改为遵循 Harness 配置，不再固定三项上限 |
| `e2b8b8d` | **预接线待交付域子路由**：`router.py` 对 storyboard/scripts/assets/media/edits/conversations 的 `api.py` 采用“文件存在即挂载”（文件存在性 + `find_spec`，不用 try/except）。**域卡因此不许改 `router.py`**，交付后路由自动挂载 |
| `33bc09f` | wip：ASSET-01-a 实现与测试（20/20 通过，**未验收**） |
| `f33c301` | fix(tests)：auth 夹具 drop 时关闭外键（修复测试顺序依赖，暴露原因是新的 assets 测试留下引用行） |
| `11cddd5` | 本交接单 |

新会话注意：
- 每次派发前后不要跑全量 `pytest tests/studio`（约 3 分钟）。域内单卡只跑自己的测试文件；
  只有改动 `app_factory.py` / `router.py` / `models.py` / `core` 或整批收尾时才跑一次全量。
- 已落盘但未验收：`backend/studio/assets/{service,api}.py`、`tests/studio/test_assets.py`；
  `backend/studio/scripts/service.py`（只有服务层，**无 api.py、无 test_scripts.py**）。
- 运行中的 3011 后端是旧进程，不加载新加的文件；改动路由/模型后需重启才算验证。

## 3. 本轮已冻结的接口决定（主模型裁定，供派发单直接引用）

以下是在附录冻结内容之上，主模型为可实施性补齐的决定；契约文档未覆盖处按此执行，
若与附录冲突以附录为准并回报。

### 通用
- ObjectRef = `{"kind","id","revision"}`；实体 id 一律服务端 ULID（`core.db.make_ulid`）。
- 写命令请求体必含 `command_id`；重放返回首次 `result_payload` + HTTP 200，无副作用；
  同 `command_id` 不同内容 → 422 `validation_failed`（field=`command_id`, rule=`reused`）。
- 域内 `api.py` 写**完整路径**（`/projects/{pid}/...`），与 `projects/api.py` 同风格。
- 单对象读 200 = 完整对象 DTO；列表 200 `{"items":[...],"next_cursor":...}`，默认 50、上限 200，
  非法 cursor → 422 `validation_failed`。
- 新建剧本对象、新建资产、草稿编辑在 00 §5“不需要 preview”清单内，但仍执行 CAS 与幂等。

### SCRIPT-01-a 新建剧本草稿对象
- `POST /api/studio/projects/{pid}/script-objects`
- 请求：`fragment_id`、`kind`(`action|dialogue`)、`text`、`speaker?`、`adaptation_note?`、`seq?|null`、`command_id`
- 201 响应体（主模型冻结，节选 SC3 字段集去掉采用相关字段）：
  `{"object_ref":{"kind":"script_object","id","revision":1},"kind","text","speaker","adaptation_note","seq","fragment":{"kind":"fragment","id","revision"},"current_revision":1}`
- `seq` 缺省或 `null` = 片段内 `max(seq)+1`（无则 1）；显式给出且 `(fragment_id,seq)` 已占用
  → 422 `validation_failed`（field=`seq`, rule=`taken`，message 带已占用对象 id）。
- 422 `precondition_failed`（片段非 confirmed / 未绑定 active 正文 / 已 retired，details
  `{"reason","blocked_by":{"kind":"fragment","id","revision"}}`）；404 项目非属主；401 未登录。
- 写 `ScriptObject` + `ScriptRevision(revision=1, source_fragment_revision=片段当前 revision)`，同事务。
- 禁止：不写 `ScriptAdoption`、不启动生成、不写 stage/状态列。

### ASSET-01-a 创建人物身份
- `POST /api/studio/projects/{pid}/assets`
- 请求：`type`(`character|costume|scene`)、`name`、`character_id?`、`spec`、`command_id`
- 201 响应体 = 与详情读同形：
  `{"object_ref":{"kind":"asset","id","revision":1},"type","name","character":<ref|null>,"current_revision":{"revision":1,"spec":{},"media_artifact_id":null,"review_status":"unreviewed"},"versions":[...],"occurrences":[],"bindings":[]}`
- spec 校验：`character` 必含非空 `appearance`；`body_type` 仅 `natural|clothed`；`costume` 至少一个非空键；
  `scene` 至少一个非空键。自由扩展键允许。
- 422 `precondition_failed`：costume 缺 character_id / character|scene 带 character_id /
  character_id 非本项目 character。422 `validation_failed`：type 非法、name 空或 >120、spec 缺必填键。
- 禁止：不改名、不加版本、不审核、不生成媒体（`media_artifact_id` 必须 null）、不写关系表、不自动采用。

### MEDIA-01 私有媒体落盘与完整性
- **无 HTTP 面**（白名单不含 api.py；媒体读取属 MEDIA-02）。
- 私有存储路径 `data/studio/media/{projectId}/`，文件名 `{artifactId}.mp4`（按实际扩展名）；
  写入 = 临时文件 + 原子 rename；入库即算 `sha256` 记入 `MediaArtifact`。
- 允许输入：mp4(h264/aac)、webm(vp8|vp9/opus)、mov、m4a、wav、mp3；其余 422 `validation_failed`（附扩展名）。
- 探测：静态构建无 ffprobe，用 `ffmpeg -i` 的 stderr 解析时长/流/尺寸，封装
  `backend/infrastructure/media/probe.py`（该新包路径在项目结构中定义为“经核对的纯文件/probe/导出工具”，
  允许存在；infrastructure 层不得写旧 Task/Record、不得改采用/确认状态）。
- 禁止：私有媒体落公开目录；写失败留“伪就绪记录”；本轮不物理删除；不新增/升级 ffmpeg。

### EDIT-01 项目剪辑草稿读写
- 主模型冻结路径：`GET /api/studio/projects/{pid}/edit-drafts/{edit_id}`、
  `PUT` 同路径（保存）、`GET /api/studio/projects/{pid}/edit-drafts`（列表）。
- PUT 请求：`{"manifest":{"instances":[{instance_id,media_id,source_in_ms,source_out_ms,speed,timeline_start_ms,track_id}]},"expected_cas_revision":int,"command_id":str}`
- 校验：`timeline_start_ms>=0`、`source_out_ms>source_in_ms>=0`、`speed>0`、`track_id>=0`、
  会话内 `instance_id` 唯一、`media_id` 必须属于本项目 `studio_media_artifacts`；
  违规 422 `validation_failed`。
- CAS：`expected_cas_revision` 必须等于当前 `cas_revision`，否则 409 `revision_conflict`
  （details `{"object":{"kind":"edit","id"},"expected","actual"}`）；成功后 `cas_revision+1`、`edit_revision+1`，
  `manifest_digest = sha256(JSON sort_keys=True, separators=(',',':'), ensure_ascii=False)`。
- 首次 PUT 不存在的 `edit_id`：`expected_cas_revision==0` 时创建（`edit_revision=1,cas_revision=1`），否则 409。
- 保存时先删该 draft 的实例行再按 manifest 重建，同事务。
- 禁止：不写 `ConfirmedEdit`、不调 ffmpeg、不改 media 表、不在 GET 建草稿。

### CHAT-01 会话和消息API
- `POST /api/studio/projects/{pid}/conversations`：按 `(project_id,scope_kind,scope_id,owner_id)`
  取或建（已存在 200、新建 201）。
- `GET .../conversations/{cid}`：`{"conversation":{...},"messages":[...],"proposals":[...]}`。
- `POST .../conversations/{cid}/messages`：追加 user 消息（只追加）；`client_message_id` 幂等
  （同会话重放 200 + 原消息，不新增行；不得新增表/列）；`sender` 由服务端固定 `user`。
  另提供内部 `append_assistant_message`，**不暴露 HTTP 写入口**。
- `GET .../conversations?scope_kind=&scope_id=&cursor=&limit=` 与
  `GET .../conversations/{cid}/messages?cursor=&limit=`：分页按 id DESC、默认 50、上限 200。
- 内部 `build_context(conversation)` 返回“作用域对象 + 最近 20 条消息”，只读。
- 失败：401；404（项目/会话/scope 对象不存在或非本属主）；403（他人会话）；422（scope_kind 非法、
  content 空或 >4000、refs 形状错）。
- 禁止：不调 LLM/供应商、不写提案应用、不新增表列、GET 无副作用、不在发送路径写 assistant 回复。

## 4. 剩余派发范围（6 张卡；白名单互不重叠）

| 卡 | 状态 | 白名单 |
| --- | --- | --- |
| ASSET-01-a | 已落盘待**验收**（20/20 绿） | 已提交 |
| SCRIPT-01-a | 服务层已落盘，缺 `api.py` + `test_scripts.py` | `backend/studio/scripts/{service,api}.py`、`tests/studio/test_scripts.py` |
| MEDIA-01 | 未开始 | `backend/studio/media/store.py`、`backend/infrastructure/media/probe.py`、`tests/studio/test_media.py` |
| EDIT-01 | 未开始 | `backend/studio/edits/{service,api}.py`、`tests/studio/test_edits.py` |
| CHAT-01 | 未开始 | `backend/studio/conversations/{service,api}.py`、`tests/studio/test_conversations.py` |
| SOURCE-14-h | 未开始（前端） | `web/features/studio/source/components/{FragmentActions,SourceChangeReview}.tsx`、`web/features/studio/source/api.ts` |

派发单必填项（照 `execution/executor-prompt.md`）：task_id、实施目录、基线 commit、并行任务与
文件归属、隔离资源、契约版本、允许读取的上下文、白名单、唯一目标、验证命令、负向案例、交付格式。
执行器禁止 commit/切分支/改 router.py/改 models.py/改账本。

MEDIA-01 与 EDIT-01 都涉及 `studio_media_artifacts`：EDIT-01 只读引用该表（FK 校验），
MEDIA-01 只写文件与工件行，二者可并行；但 EDIT-01 的测试若需要一个已存在的媒体工件，
必须自行插入最小 `MediaArtifact` 行或在测试内跳过，不得依赖 MEDIA-01 的产物。

## 5. 验收与入账（由新会话的主模型执行）

1. 执行器 submitted 后，逐项审 diff 与越界情况；跑该卡测试文件 + 必要的负向复验。
2. 独立复验建议：`TestClient` 真调接口 + 直接查库核对行数与 revision；
   MEDIA-01 另需真实本地样本文件与损坏文件用例。
3. 通过后按“单卡单提交”集成并写账本 `acceptance`（含 tested_baseline=当前 HEAD、
   checks、evidence），父卡在全部子卡通过后才 accepted。
4. 整批收尾跑一次全量 `pytest tests/studio`，并重启 3011 后端确认路由挂载。
