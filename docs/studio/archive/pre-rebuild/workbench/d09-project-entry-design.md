> **历史记录，已退出当前设计与实施依据。** 文中版本、完成状态、旧入口及迁移方案仅描述当时背景；当前规则从[文档入口](../../../README.md)读取。

# D09 项目入口（多用户）· 设计与实施计划（v1.0 · 已实施）

> **状态：v1.0 —— 已实施（P0–P4 完成 + 原子切换上线）**。全部决策已锁定并落地：**Q1 账号**（PBKDF2 哈希 + 每次登录轮换 bearer token + `sl_auth` cookie）· **Q3 故事由项目管理**（项目私有 `story_source`，生产管线零改动；旧故事市场/`/import` 整体删除**已完成**，见 §11 P3 收尾）· **Q4 干净切割**（已执行：清旧账号 + 创作内容，备份 `data/backups/` 兜底）· **Q5 `?work` 属主校验**（全部 `/{pid}/*` 端点经 `get_work` 属主校验）· **Q6 `Task.owner` 保留**（worker）· **Q7 删匿名会话 key** · **Q8 供应商密钥→独立后端+扣豆**（独立轨，不在 D09）。不并入已交付的 F0–F4 框架。实施细节见 §11。
>
> 依据：`canvas-design-direction.md`（§2/§6）、`design-task-skeleton.md`（D03/D09）、`design-spec.md`；现状代码 `backend/`（`db.py`/`auth.py`/`authors.py`/`catalog.py`/`story_sources.py`/`reader_branch.py`/`worker.py`/`providers.py`/`environment.py`）、`web/app/ProjectHome.tsx`、`web/app/auth.ts`、`web/app/watch/`、`web/app/author/workbench/`。

---

## 0. 目标

把站点主页从「**原作导入 + 故事市场**」（按原作/微小说组织）重构为「**项目入口**」（按**公开项目**组织 + **新建项目** + **我的项目**），并在**整套账号系统**下运行：每个项目有明确**属主**，"公开"是显式**可见性**，"我的项目"按属主隔离。**独立"故事"实体（`story_source`）与 `/import` 流整体删除**（原文改由项目内 D03 管理）；**旧的匿名访客身份/会话 key 一并删除**，身份统一走真实账号。

| #   | 已定决策                     | 一句话                                                                                                    |
| --- | ---------------------------- | --------------------------------------------------------------------------------------------------------- |
| 1   | **B1 整套账号系统**          | 真实注册 + 登录（账号实体 + 会话 token + 密码哈希）+ 项目属主 + 公开注册表 + 权限                         |
| 2   | **删除"故事"实体**           | 删 `story_source` 数据 + `/import` 流；原文由项目内 D03 导入/切分管理；上游微小说浏览下线                 |
| 3   | **严格"先建空项目再加原文"** | 先建无源项目 → D03 故事总控导入原文并切分；不从公开卡片隐式建                                             |
| 4   | **干净切割**                 | 一次性**清空旧账号 + 创作内容**（不迁移、不归默认账号），重置为空后从真实账号起步                         |
| 5   | **删除旧匿名身份 key**       | 删 `Task.session_id`（payer）+ `reader_session_id`/`X-Reader-Session`（匿名阅读者会话）；临时改写改挂账号 |
| 6   | **模型接入不作永久机制**     | 供应商 `.env` 密钥 → 未来**独立后端（管理扣豆/豆余额）**；本期抽象接入层、不硬依赖（§2.5，独立轨）        |

---

## 1. 现状基线（改动前）

- **单一本地作者、无账号**：`authors.py:1` "every project belongs to the single local maker"；`app.py` 无任何登录/账号路由。
- **数据模型**（`db.py`）：
  - `Record{ id, kind, data(JSON), version, created }` —— **无属主、无可见性**；`kind ∈ { author_project, story_source, reader_release, reader_branch, audit, worker_heartbeat, … }`。
  - `Task{ …, session_id, owner, lease, … }` —— `session_id` 是**遗留"每访客 payer/session"（空、不生效）**；`owner` 是**后台 worker 进程身份**（`worker.py: owner=uid('worker')`，用于抢锁/租约，**不是用户**）。
- **故事来源**（`story_sources.py`）：`stories()`（**上游微小说列表**，带缓存/`fetched_at`）+ `POST /import`、`POST /import-file`、`GET /imports`、`GET /{work_id}`；对应 `Record.kind='story_source'`。
- **匿名阅读者会话**（`reader_branch.py`）：临时改写按 **`reader_session_id`**（取自 `X-Reader-Session`）隔离；`reader_branch` 记录存 `reader_session_id` + `release_id`。
- **供应商密钥**（`environment.py`/`providers.py`）：`LLM_API_KEY`/`IMAGE_API_KEY`/`VIDEO_API_KEY` 来自运营者 `.env.local`——**运营者自己的模型配置**（`model_config()` 返回该本地配置）。
- **其它接口**：`GET /api/author/projects`；`GET /api/reader/catalog`（story_sources + 项目 + `reader_release`）；`POST /stories/{work_id}/open`、`/import`（由原作建项目）；`POST …/projects/{pid}/publish`。
- **主页**（`ReaderExperience`，暗色 reader-world）：`ReaderHero` + **"故事市场"货架**（`ReaderStoryGrid` 按**原作**）+ **大幅导入区**（`StoryImport`）+ `reader-screen`（观看 + 临时改写）。**浏览单位 = 原作/微小说。**
- **已交付框架**：F0–F4 工作台（`/author/workbench?work=<id>`，单项目、以项目为核心）。

---

## 2. 已定决策的落地含义

### 2.1 B1 整套账号系统

- 新增**账号实体**（建议独立 `users` 表：`id, username, email, password_hash, created`；**豆余额**由独立后端记账，见 §2.5）。
- **注册 / 登录 / 会话**：`POST /auth/register`、`POST /auth/login` → 签发 **token**（cookie/localStorage）；每请求解析**当前用户**。
- `Record.owner_id` = 当前用户 id；`visibility` = `private` | `public`。
- **删除旧匿名身份 key**：
  - 删 `Task.session_id`（遗留 payer/session）。
  - 删 `reader_session_id` / `X-Reader-Session`（匿名阅读者会话）；**临时改写的会话改挂登录账号**（branch 归属 (账号, release)）。
- **干净切割（Q4）**：**不保留/迁移**旧账号与创作内容；内容**重置为空**后从真实账号起步（首个注册者 = 首个账号，**无"默认本地账号"**，见 §3）。

### 2.2 删除"故事"实体（`story_source` + `/import`）

- **删除**：`Record.kind='story_source'` 数据；`story_sources.py`（`/import`、`/import-file`、`/imports`、`/{work_id}`）；主页故事市场货架 + 大幅导入区。
- **原文改由项目管理**：D03 故事总控在**项目内**导入/粘贴原文 → 切分为**片段**（替代旧"建 `story_source`"）。
- **上游微小说浏览下线**：`stories()` 作为主页浏览源移除（如需"从上游导入"，降级为 D03 的一个来源，另议）。
- **保留**：`reader_release`（成片）+ 观看 / 临时改写（改为**由 release/账号**驱动，不再依赖 `story_source` / 匿名会话）。

### 2.3 严格"先建空项目再加原文"

- 新增 **`POST /api/author/projects`**（建**空项目**：仅 `name`，无 source）→ 返回项目 id。
- → 工作台**故事总控（D03）**：导入原文 → 切分片段 → 确认设定。
- **不**从公开项目卡片隐式触发建项目。

### 2.4 身份/密钥的去留（避免误删）

| 机制                                     | 语义                                  | 处置                                    |
| ---------------------------------------- | ------------------------------------- | --------------------------------------- |
| `Task.session_id`                        | 遗留"每访客 payer/session"            | **删除**                                |
| `reader_session_id` / `X-Reader-Session` | 匿名阅读者会话（临时改写）            | **删除**，改挂账号                      |
| `Task.owner`                             | 后台 **worker 进程**身份（抢锁/租约） | **保留**（非用户）                      |
| `LLM/IMAGE/VIDEO_API_KEY`                | 运营者的模型密钥（`.env.local`）      | **不作永久机制** → 未来独立后端（§2.5） |
| 真实账号（`users`）+ 会话 token          | 新的统一身份                          | **新增**                                |

### 2.5 未来：模型接入独立后端 + 扣豆（独立轨，不在 D09 P0–P4 内）

- **现状**：模型调用走运营者 `.env.local` 的 `LLM/IMAGE/VIDEO_API_KEY`（`environment.py` / `providers.py` / `image_provider.py`）。
- **未来**：**替换为独立后端**——统一提供模型调用，并**管理"扣豆"**（豆/积分余额，按生成扣减、充值）。
- **与账号的关系**：**账号携带豆余额**（由独立后端记账）；生成内容 = 从该账号豆余额扣减。
- **D09 边界**：本期**不建**独立后端，也**不把本地 `.env` 密钥当永久机制**——**抽象模型接入层**（`model_config()` 的 provider 来源可替换），使其未来指向独立后端。**豆余额/扣豆的记账设计属该独立后端轨，另行立项。**

---

## 3. 数据模型改造

- **新增 `users` 表**（独立，便于登录/索引）：`id, username, email, password_hash, created`（豆余额由独立后端管，见 §2.5）。
- **`Record` 增列**（一次性迁移 + 回填）：`owner_id`（**索引**，default `local`）、`visibility`（default `private`）。
- **`Task`**：删 `session_id` 列；**保留** `owner`（worker）。
- **属主/可见性规则**：`author_project.owner_id` = 建项目者；`reader_release` **继承所属项目**；`story_source` → **删除**；`reader_branch` 由 `reader_session_id` 改挂**账号**（`(账号, release)`）。
- **数据处置 = 干净切割（Q4）**：
  1. **一次性清空**所有旧账号 + 创作内容：删除 `author_project` / `story_source` / `reader_release` / `reader_branch` / `audit` / `Task` / `worker_heartbeat` 记录——**不迁移、不归"默认本地账号"**（不做"原文内联进项目"那类保留）。
  2. 删 `Task.session_id` 列；`reader_session_id` 随内容清空自然消失。
  3. 表结构保留；系统从**空**起步，**首个注册者 = 首个账号**。
  4. **wipe 前备份 `story.db`**（demo 内容可逆兜底）。
  - **时机**：推荐在 **P0 前一次性执行**（最干净）；也可随 **P3 主页翻转**执行（过渡期旧主页仍可看）。两种最终系统都**无旧账号/内容**。

---

## 4. 身份、权限与公开边界

- **公开项目注册表**：`GET /api/author/projects?scope=public`（或独立 `GET /api/public-projects`）—— 返回 `visibility=public` 项目卡片（封面 / 名称 / 作者 / 简介 / 已发布成片数）。
- **权限矩阵**：

  | 主体     | 自己项目              | 他人 public 项目                  | 他人 private 项目 |
  | -------- | --------------------- | --------------------------------- | ----------------- |
  | 属主     | 读 / 写 / 发布 / 删除 | —                                 | —                 |
  | 其他用户 | —                     | **只读**（公开介绍 + 已发布成片） | 不可见            |

- **公开边界（§9-Q2）**：发布 ≠ 全公开。默认公开 = **项目公开介绍 + 已发布成片**；原文 / 片段 / 设定 / 剧本 / 分镜 / 资产**默认不公开**。
- **观看 / 临时改写门槛（§9-Q2，默认）**：观看他人 public 成片 **免登录**（只读公开对所有人可见）；**临时改写需登录**（branch 归账号，不落地、不影响他人项目）。

---

## 5. 新建项目流程（严格）

```
[新建项目]
   → POST /api/author/projects { name }        （owner = 当前用户, visibility = private）
   → /author/workbench?work=<pid> 「故事 / 故事总控」
   → D03：项目内导入 / 粘贴原文 → 切分为片段（确认设定）
   → 「制作」（改编剧本 / 分镜）……
```

- 建项目**不要求**先有原文；原文在故事总控内**后补**（替代旧 `/import`）。

---

## 6. 主页（项目入口）UI

- **顶栏**：品牌 + [**新建项目**]（主操作）+ [**我的项目**] + 搜索 + 主题切换（+ 登录/账号入口）。
- **主体（浏览单位 = 项目）**：
  - **公开项目列表**：一项目一卡（封面 / 名称 / 作者 / 简介）；点进 → **项目公开介绍 + 已发布成片**（一项目多成片，归一卡）。**"查看他人"即浏览这些 public 项目。**
  - **我的项目**：恢复 / 继续（`GET /author/projects`，owner = 我）。
  - **两个独立空态**：① 无公开项目 ② 我没有项目。
- **删除**：故事市场货架（按原作）、大幅导入区、重复导入按钮、上游微小说浏览。
- **保留**：`reader-screen` 观看 + 临时改写（从"某项目已发布成片"进入，会话改挂账号）。
- **主题**：沿用整站双主题（reader-world 暗色 + 日/夜 D08）。

---

## 7. 后端改造清单

| 类型 | 项                                                                    | 说明                                                                  |
| ---- | --------------------------------------------------------------------- | --------------------------------------------------------------------- |
| 新增 | **账号系统**                                                          | `users` 表 + `/auth/register` + `/auth/login` + 会话 token + 密码哈希 |
| 新增 | `POST /author/projects`（空项目）                                     | 严格新建入口                                                          |
| 新增 | public 项目列表                                                       | `?scope=public` 或独立路由                                            |
| 改造 | `Record` + `owner_id`/`visibility` 增列                               | §3                                                                    |
| 重置 | **干净切割**：清空旧账号+内容、删 `Task.session_id` 列（备份后）      | §3/Q4                                                                 |
| 删除 | `reader_session_id`/`X-Reader-Session`                                | §2.1/§2.4                                                             |
| 删除 | `story_sources.py`（`/import` 等）+ 上游 `stories()` 浏览             | §2.2                                                                  |
| 改造 | author/* **写**路由加属主鉴权；`/reader/catalog` → 项目维度（或废弃） | 主页浏览单位 原作 → 项目                                              |
| 抽象 | **模型接入层**（`model_config` provider 来源）                        | 未来指向独立后端 + 扣豆（§2.5）；本期只保证可替换                     |
| 依赖 | **D03 故事总控**（项目内导入原文 + 切分）                             | 独立排期                                                              |
| 不动 | 媒体 / 剪辑内核（D06）、`Task.owner`(worker)                          | 与身份改造正交 / 保留                                                 |

---

## 8. 分阶段计划（建议，供调整）

> 依赖 **P0 → P1 → P2 → P3 → P4**，每阶段独立可验收。**干净切割**推荐在 **P0 前一次性执行**（或随 P3 主页翻转）；`story_source` 代码删除在 P3。

- **P0 前 · 数据重置**：**备份 `story.db`** → 清空旧账号 + 创作内容（Q4 干净切割）。
- **P0 账号系统**：`users` + 注册/登录/token/密码哈希 + `Record` 加 `owner_id`/`visibility` + 删 `Task.session_id` 列 + author/* 属主鉴权。
- **P1 可见性与公开注册表**：public 项目列表 + 权限矩阵 + 公开边界 / 观看门槛落地。
- **P2 建空项目 + 故事总控**：`POST /projects`（空）+ **D03** 项目内导入原文 / 切分片段。
- **P3 主页项目入口 + 删故事实体**：按 §6 重做主页；**删除 `story_source` 数据 & `story_sources.py`** + catalog 重组/废弃 + **删 `reader_session_id`/`X-Reader-Session`**（临时改写改挂账号）。
- **P4 接线与收敛**：观看 → 项目成片、临时改写归属、工作台 `?work` 属主校验、权限边界收敛。
- **独立轨（非 P0–P4）**：模型接入**独立后端 + 扣豆/豆余额**（§2.5）另行立项。

---

## 9. 决策记录（已全部锁定）

- **Q1 身份形态** → **真实注册 + 登录（整套账号系统）。**
- **Q2 公开边界 / 观看门槛** → 默认公开 = 项目介绍 + 已发布成片，其余 private；**观看他人 public 成片免登录；临时改写需登录**。
- **Q3 删除边界** → **删 `story_source` + `/import`；故事由项目管理（D03 项目内原文切分）。**
- **Q4 数据处置** → **干净切割**：一次性清空旧账号 + 创作内容，**不迁移/保留**；备份 `story.db` 后执行。
- **Q5 工作台** → `?work=<id>` 加**属主校验**（他人项目只读、不进编辑），P4 落地。
- **Q6 `Task.owner`** → **保留**（worker 抢锁/租约），不复用为用户属主（用户属主用 `Record.owner_id`）。
- **Q7 匿名身份 key** → **删 `Task.session_id` + `reader_session_id`/`X-Reader-Session`**；改挂真实账号。
- **Q8 供应商密钥** → **不作永久机制**，未来独立后端 + 扣豆；本期抽象接入层。

---

## 10. 风险

- **性质根本转变**：从"无账号本地 demo" → "账号制多用户"；所有 author/* 写路由、`/reader/catalog`、`reader-screen`、workbench 都受属主/可见性影响。
- **删除牵动面广**：`story_source`（主页/catalog 强依赖，P3 同批删）、`reader_session_id`（临时改写）、`Task.session_id` 均需配套改读写路径，不能单独拔除。
- **干净切割是破坏性操作**：清空旧账号 + 创作内容**不可逆**；**执行前必须备份 `story.db`**（可恢复）。
- **上游微小说浏览下线**：现有"浏览官方/上游微小说"能力消失（如需"从上游导入"，作 D03 来源另议）。
- **模型接入是独立未来轨**：供应商 key → 独立后端 + 扣豆；D09 只保证"可替换"，不落地它；豆余额/扣豆记账属独立后端，另行立项。
- **依赖未实现的 D03**（项目内原文导入/切分）。

---

## 11. 实施状态（v1.0）

> 截至本次定稿，D09 **P0–P4 全部实现并原子切换上线**（:8010 新后端 + :3001 新前端）；**旧故事市场整体删除亦已完成**（P3 收尾，见下）。

- **P0 账号系统**：`users` 表（`id/username/email/password_hash/auth_token/created`）；PBKDF2-HMAC-SHA256（21 万轮）密码哈希；注册 / 登录（**每次登录轮换 bearer token**）/ `me`；`Authorization: Bearer` 或 `sl_auth` cookie 双通道；**`request_ctx`（contextvar）** 承载当前用户——有请求上下文则强制属主（401 无 token / 403 非属主），worker 无上下文则放行；删 `Task.session_id` 列；`Record` 增 `owner_id`（default `local`，索引）+ `visibility`（default `private`），`apply_migrations()` 幂等迁移。
- **P1 可见性 + 公开注册表**：`GET /author/projects?scope=public`（免登录，公开项目卡片含 `creator` 归属）；`?scope=mine`（属主，登录）；`POST /author/projects/{pid}/visibility`（属主切换公开/私有 + 简介）。
- **P2 建空项目 + 故事由项目管理**：`POST /author/projects`（属主，建空项目，`stage=story`）；`POST /author/projects/{pid}/story`（属主，在项目内导入原文 → 建**项目私有** `story_source`）；生产管线 `director.create(source_id=…)` **零改动**即跑通（故事由项目私有容器承载）。
- **P3 主页项目入口 + 干净切割**：新主页 `ProjectHome`（顶栏 品牌 / 我的项目 / 新建项目 + 右上 登录 / 注册；主体 我的项目 + 公开项目）+ `auth.ts`（`sl_auth` cookie + localStorage，使**未改动的**工作台 fetch 经同源 cookie 通过属主校验）；**干净切割已执行**（清旧账号 + 旧创作内容，仅留 `worker_heartbeat`；备份 `data/backups/story.db-20261003-174953` 兜底）；**原子切换**：停旧 :8010 → 清库 → 起新 :8010 → :3001 前后端对齐。
- **P4 接线与收敛**：**工作台 `?work` 属主校验**——全部 `/author/projects/{pid}/*`（含 SSE `events` 经 `project_progress` → `get_work`）均属主校验；**公开成片观看器**——`GET /author/releases/{rid}`（**免登录**，按发布项目 `visibility` 门控）+ 新路由 `/watch?release=<id>`（顺序播 `entries`）+ 主页公开详情「观看成片」入口。
- **P3 收尾（已完成）**：删除旧**故事市场**整体——后端 `open_story` + `work_id_for`（`authors`）、`story_sources` 的 `/import`·`/import-file`·`/{work_id}`·`/imports` 路由 + `stories()`/`detail()`/`import_story()`（**仅保留 `_check_content`**）、整个 `catalog` 模块（`/api/reader/catalog`）+ `app.py` 的 `story_router`/`catalog_router` 注册；前端删除 `ReaderExperience` + `StoryImport` + `ReaderHero` + `reader-types` 的 `productionLink`/`groupProductionsByStory`、工作台 `?story=` 旧分支（`/?story=` 观看链接改指 `/watch?release=`）。**保留 `story_source` 记录类型**（项目私有故事容器，管线依赖）；tsc 0 + 前后端市场引用零残留。
- **独立轨（非 D09）**：模型接入 → 独立后端 + 扣豆 / 豆余额（§2.5）另行立项；本期不落地，仅保证接入层可替换。

---

_v1.0 —— 已实施（P0–P4 + 原子切换上线 + 旧故事市场整体删除 P3 收尾，全部完成）。_
