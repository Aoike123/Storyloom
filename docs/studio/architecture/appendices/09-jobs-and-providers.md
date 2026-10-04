# 附录 09 · StudioJob 任务族与供应商适配器（ROOT-04）

版本：2.2 · ROOT-04 冻结 · 2026-10-04。覆盖：任务族表（DB-08）、job kind 注册表、租约/重试/取消、计费边界（R08）、供应商适配器抽取白名单（无旧 Record/Task/stage 依赖）。依据：[产品规范 R08/R09](../../product/product-spec.md)、[领域契约 §5/§6](../domain-contracts.md)、旧栈只读盘点 [inventory](../../evidence/legacy-dependency-inventory.md)。

实施位置：`backend/studio/jobs/`（models/schemas/service/api + `runner.py` + `worker_main.py` + `handlers/`）；纯适配器 `backend/infrastructure/providers/`；配置读取 `backend/core/config.py`（BASE-03-d）。

## 1. 表（DB-08）

```text
studio_jobs
  id            PK ULID
  project_id    FK studio_projects
  owner_id      FK users
  kind          TEXT   -- shot_generate | asset_generate | transcode | export | release_publish
  ref_kind/ref_id     -- 目标业务对象（shot/asset/media/edit/release…）
  payload       JSON   -- 该 kind 的输入（§3）
  status        TEXT   -- pending | running | succeeded | failed | cancelled
  priority      INT default 0
  created_at / submitted_by
  lease_owner   TEXT nullable      -- worker 标识
  lease_expires_at  TIMESTAMPTZ nullable
  attempts      INT default 0
  usage         JSON nullable      -- 汇总 usage（§4）
  last_error    TEXT nullable
  finished_at   TIMESTAMPTZ nullable

studio_job_attempts
  id            PK ULID
  job_id        FK studio_jobs
  seq           INT                -- 从 1 递增
  lease_owner   TEXT
  started_at / finished_at
  provider_request_id  TEXT nullable   -- 厂商任务号（minimax 等）
  client_request_id    TEXT ULID       -- 我方请求 id（LLM/图片无厂商号时）
  status        TEXT   -- running | succeeded | failed | cancelled | indeterminate
  usage         JSON nullable        -- 厂商返回的 usage（tokens/images/duration…）
  cost          JSON nullable        -- 可确认时记实际成本，否则 null
  error_tail    TEXT nullable        -- stderr/异常尾 4KB
  UNIQUE (job_id, seq)

studio_job_events
  id            PK ULID
  job_id        FK studio_jobs
  type          TEXT   -- created | claimed | progress | provider_submitted | succeeded | failed | cancelled | indeterminate
  at            TIMESTAMPTZ
  detail        JSON
```

- **StudioJob 是新流程唯一状态权威**（验收）：进度/状态/计费/恢复全部来自这三张表；无旧 Task/stage/node/读者分支任何字段。
- 事件只追加；`indeterminate` 是独立终态（§4）。

## 2. 生命周期与租约

- `pending` → worker `claim`（按 kind→lane 过滤，`FOR UPDATE SKIP LOCKED` 语义用事务内 CAS：`lease_owner=:w AND lease_expires_at>now` → 置新租约）→ `running`。
- 租约时长 60s，worker 每 15s 心跳续期；**租约过期 = 可被重新 claim**（本地恢复手段，R09：导出失败可同版重试）。
- 取消：运行中 → 杀 ffmpeg 进程组/中断 httpx 流 → `cancelled` + 事件；`pending` 取消 = 直接终态。
- 失败重试策略（每 kind）：`export/transcode/release_publish`（纯本地）可自动重试 1 次（新 attempt，同 payload）；`shot_generate/asset_generate`（**收费**）**不自动重试**（§4）。
- worker：`python -m backend.studio.jobs.worker_main`（ROOT-01 的启动方式即此；compose 可选 service）；单进程轮询 500ms；lane：asset/video/export/local。

## 3. job kind 注册表（全部新操作在此登记；同步命令见附录 01–08 命令表）

| kind | 触发 | 前置（缺失即 409/422） | 输入 payload | 产物 | 用户确认点 | 恢复 |
| --- | --- | --- | --- | --- | --- | --- |
| `asset_generate` | AS2/AS3/AS4（创建/换规格/重生图） | 资产可写、图片供应商已配置 | {asset_revision_id, prompt, image_size, references[]≤9, steps} | MediaArtifact(png) + revision.media_artifact_id | 直接（R09 资产生成入口直接执行；失败可同版重试） | 租约重 claim；收费不自动重试 |
| `shot_generate` | SB9 preview→apply | 镜头存在且输入齐备（adoption+binding 冻结快照）、视频供应商已配置 | {shot_revision_id, video_prompt, image_refs[]≤6, duration_s∈[6,10]} | MediaArtifact(mp4, 已转码归一) + shot_media | **preview 必须**（kind=shot_generate，§00 §5） | 同左；厂商任务号保留后只轮询 |
| `transcode` | 任意外源媒体入库（上传/供应商产物） | 文件已落地私有存储 | {artifact_id, from_codec, to=mp4/h264/aac} | 归一 MediaArtifact | 无（内部步骤） | 本地自动重试 1 次 |
| `export` | EDIT-05/06（编辑导出） | ConfirmedEdit 存在 | {confirmed_edit_id, manifest_digest} | 成片 MediaArtifact(mp4) | **preview 必须**（kind=edit_confirm→导出） | 同版重试（新 job 同 ConfirmedEdit） |
| `release_publish` | RV5（发布 preview→apply） | Release 存在、poster 与 confirmed_edit 齐备 | {release_id, files[]} | 公开目录文件 + Release=published | **preview 必须**（kind=release_publish） | 本地自动重试 1 次 |

- 注册表外的 kind 一律 422 `validation_failed`（`unknown_job_kind`）；**不得出现旧节点名/stage 名**（验收）。
- 同步命令（P1…RV5 中非 preview 部分）不建 job，直接命令事务。

## 4. 计费边界（R08，验收：不确定提交不自动重复收费调用）

- **提交前**：attempt 建 `client_request_id`（LLM/图片）；视频提交成功后立即记厂商 `provider_request_id` 并写 `provider_submitted` 事件。
- **结果不确定**（网络中断/超时且未拿到厂商号或最终状态）→ attempt `indeterminate`，job 停在该 attempt：
  - 有厂商任务号 → worker **只轮询**该号，绝不重新提交（R08：已有编号继续轮询）。
  - 无厂商号 → **不自动重试**；job 详情显示“结果不确定，已保留记录”+ **人工确认重试**按钮（用户确认后才新建 attempt，计费由用户知情承担）。
- **计费确认**：`usage` 记录厂商返回的实际用量（tokens / images:1 / duration_s）；`cost` 能确认记实际值，不能确认 = `null`；UI 显示“实际成本/未知成本”，**不显示虚构金额**（R08）。
- 配置门槛：未配置供应商的 job kind 提交即 422 + 具体缺失项（沿用旧 `payment_message` 语义，迁到 `core/config.py`）。

## 5. 供应商适配器抽取白名单（`backend/infrastructure/providers/`）

从旧栈**只读抽取纯协议**，全部改写依赖，禁止 import 旧 `providers.py/image_provider.py/llm_stream.py/workflows.py/worker.py/db.py` 业务路径：

| 新模块 | 抽取自 | 保留的纯逻辑 | 移除/改接 |
| --- | --- | --- | --- |
| `llm.py` | `llm_stream.read_completion` + `providers.chat_json` | SSE 消费、stall 超时、256KB 上限、usage 提取、JSON 模式、deepseek 禁 thinking 参数、**不自动重发** | on_event → `JobEvent(progress)`；task_id → job/attempt；错误 → ProviderError 新消息 |
| `image.py` | `image_provider.*` | siliconflow 协议（1024²文生图、参考图合成 1–9 张/steps 1–100）、https-only 下载、20MB/256px/5760px/宽高比 0.4–2.5 校验、PIL 归一 PNG | `DATA/media` → 媒体存储接口（附录 08）；`save_generation_request`/`image_errors` → attempt 字段/事件；`reference_image_model` 常量 → 本附录 §6 模型表 |
| `video.py` | `providers.submit_video/poll_video` | minimax/ark 提交+轮询协议、64MB 守卫、https 或已审本地参考 | 同上；display_summary 等旧 UI 注入移除 |
| `base.py` | `providers.endpoint` 等 | URL 必须 https 校验、错误分类 | settings()/paid_gate → `core/config.py` 等价物 |

- **不抽取**：`workflows.py`（stage/node-skill DAG）、`worker.py`（旧租约/车道/读者分支）、`provider_usage` 表、`diagnostics.py`（旧诊断 UI 能力，退役）；新流程**不绑定**旧 prompt payload 格式（新 prompt 从新领域对象规格现构建）。

## 6. 模型表（冻结，R08：不开更多付费通道）

| 用途 | 供应商 | 环境变量（沿用旧 .env 名） | 约束 |
| --- | --- | --- | --- |
| 文本/会话 | DeepSeek（OpenAI 兼容） | LLM_BASE_URL / LLM_MODEL / LLM_API_KEY | JSON 模式；stream；禁 thinking 参数 |
| 图片 | 硅基流动 | IMAGE_PROVIDER=siliconflow / IMAGE_ENDPOINT / IMAGE_MODEL / IMAGE_API_KEY | 1024x1024；参考合成 ≤9 图 |
| 视频 | MiniMax H3（或火山方舟 Tasks） | VIDEO_PROVIDER / VIDEO_ENDPOINT / VIDEO_MODEL / VIDEO_API_KEY | 时长 6–10s；参考图 ≤6 |
