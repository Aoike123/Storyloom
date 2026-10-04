# 附录 01 · 项目与不可变来源（DB-01）

版本：2.1 · ROOT-02 冻结 · 2026-10-04。覆盖：`StudioProject`、`StoryRevision` 与 `/projects`、`/sources`、`/source-changes` 命令族。共享约定（ObjectRef/CAS/幂等/错误/预览）见[附录 00](00-object-ref-and-protocol.md)。

实施位置：`backend/studio/projects/models.py`、`backend/studio/sources/models.py`（表）、`backend/studio/projects/{service,api}.py`、`backend/studio/sources/{service,api}.py`（命令）；测试 `tests/studio/test_projects.py`、`tests/studio/test_source_models.py`。

## 1. 表

### studio_projects

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | |
| owner_id | text(26) | NOT NULL，FK→accounts.id | 属主账号（BASE-03-b 表） |
| name | text(80) | NOT NULL | |
| description | text(500) | NULL | |
| visibility | text(8) | NOT NULL，CHECK IN ('private','public')，默认 'private' | 控制公开详情/观看可见性；不单独公开原文/过程 |
| active_source_revision_id | text(26) | NULL，FK→studio_source_revisions.id | 当前正文版本；首个导入自动激活 |
| created_at / updated_at | timestamptz | NOT NULL | |

- 唯一：`(owner_id, name)`。重名 → `validation_failed`（field=`name`，rule=`unique`）。
- **无 stage/阶段列**（R01/R02：导航不由后台阶段驱动）。
- 封面不在本表：公开列表/详情的封面取自该项目最新公开 Release 的 poster（Release 表见 ROOT-03/04 附录）；无公开发布时不显示封面。避免 project→asset/media 反向外键环。
- 物理删除项目本轮不提供（R11/R12 范围）；`retired` 状态不引入，项目可长期空置。

### studio_source_revisions（不可变）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | text(26) | PK，ULID | 稳定来源版本身份 |
| project_id | text(26) | NOT NULL，FK→studio_projects.id | |
| previous_revision_id | text(26) | NULL，FK→本表.id | 版本链；首版为 NULL |
| raw_content | text | NOT NULL | 提交内容原样保存：不 trim、不 Unicode 归一化 |
| raw_hash | text(64) | NOT NULL | sha256(raw_content 的 UTF-8 字节)，hex |
| canonical_content | text | NOT NULL | 仅 CRLF/CR→LF；其余字符与首尾空白保留 |
| canonical_hash | text(64) | NOT NULL | sha256(canonical 的 UTF-8 字节) |
| offset_policy | text(16) | NOT NULL，CHECK='lf-utf16-v1' | 固定常量，响应必回 |
| char_length | int | NOT NULL | canonical 的 UTF-16 code unit 数 |
| created_at | timestamptz | NOT NULL | |

- 链约束（CHECK/服务层）：`previous_revision_id` 非空时其 `project_id` 相同。
- **无 UPDATE/DELETE 命令**（R03/R11）。“当前正文”= `projects.active_source_revision_id` 所指行。
- 同一项目可有多个并存版本（重导入、正文更新）。片段/对象各自绑定具体版本，**永不自动重映射**（§3）。

## 2. 命令（`/api/studio/...`，全部需属主鉴权；公开端点另列）

| # | 方法 路径 | 请求体 | 行为与成功 | 失败 |
| --- | --- | --- | --- | --- |
| P1 | POST `/projects` | `{name, description?, command_id}` | 201 项目 DTO；`visibility='private'`，无来源。不隐式导入（R12） | 422 name 空/>80；409 属主内重名 |
| P2 | GET `/projects?cursor=&limit=` | — | 200 列表（仅当前账号）：id、name、description、visibility、active_source_revision_id、created_at、updated_at。空列表 ≠ 读失败（R12） | 401 未登录 |
| P3 | GET `/projects/{pid}` | — | 200 完整项目 DTO | 404 不存在/非属主 |
| S1 | POST `/projects/{pid}/sources` | `{content, command_id}` | 201 新 StoryRevision DTO（§1 全字段 + `is_active`）。无活跃版本→自动激活；已有活跃版本→新版本**不激活**，响应附 `activation_hint`（指向 S4/S5）。同一文本重复导入（canonical_hash 相同）仍建版本（历史依据），响应 `duplicate_content: true` | 422 content 空；409 command 重放（200 原结果） |
| S2 | GET `/projects/{pid}/sources?cursor=` | — | 200 版本列表（倒序）：id、previous_revision_id、raw_hash、canonical_hash、offset_policy、char_length、created_at、is_active、fragment_count（该版本未退役片段数，计算值） | 404 |
| S3 | GET `/projects/{pid}/sources/{revisionId}` | — | 200 完整不可变内容：raw_content、canonical_content 及各 hash/长度/策略/previous/is_active | 404 跨项目引用 |
| S4 | POST `/projects/{pid}/source-changes/preview` | `{kind:"source_activate", target_revision_id, command_id}` | 201 ChangePreview（附录 07）：baseline=`{active_revision_id, target_revision_id, target_revision 的 hash}`；impact=§3 规则 | 422 target 不存在/非本项目/已是 active；409 preview 重放 |
| S5 | POST `/projects/{pid}/source-changes/apply` | `{preview_id, command_id, expected_active_revision_id}` | 200 新项目 DTO；激活切换 + ReviewDecision | 409 `revision_conflict`（active 已变）/`preview_stale` |
| S6 | POST `/projects/{pid}/previews/{previewId}/reject` | `{command_id}` | 200 预览置 rejected（附录 07 公共命令） | 409 已终态 |

- P1–S6 全部走附录 00 的幂等与属主校验；S2/S3 为纯读。
- **激活语义（S4/S5，R03/R11）**：
  - `affected[]`：绑定到 target 版本的片段/对象（激活后成为默认工作集）。
  - `needs_review[]`：绑定到**原 active** 版本的全部未退役片段（其范围不能套进新文本，需来源复核；不承诺“只影响当前片段”）。
  - `preservable[]`：以原 active 版本为输入依据的视频、剪辑、发布（保留真实依据，继续可读）。
  - 激活不删除任何版本，不移动任何范围；apply 仅切换 `active_source_revision_id`。

## 3. 公开端点（`/api/studio/public/*`，无鉴权，白名单字段）

| 方法 路径 | 返回 | 规则 |
| --- | --- | --- |
| GET `/public/projects?cursor=&q=` | 列表：id、name、creator_name、description、has_public_release、latest_release_at | 仅 `visibility='public'` 的项目；空列表与“无个人项目”由前端分场景表达（R12） |
| GET `/public/projects/{pid}` | 详情：上列 + `releases[]`（id、name、created_at，见 ROOT-03/04 附录） | 不返回 raw/canonical 正文、片段、剧本、资产、对话；未公开项目对任何账号（含属主之外者）`404` |

公开观看 `/watch/[releaseId]` 消费 Release 产物（ROOT-03/04 附录定义媒体访问白名单），不适配旧分片发布。

## 4. 实现注记（供 DB-01 / BASE 任务）

- `studio_command_records`（附录 00 §3 表）实现于 `backend/studio/contracts/models.py`，随 BASE-01-b 交付（派发时主模型将其加入该卡白名单）；本附录表不依赖它。
- 建表顺序：accounts → studio_projects → studio_source_revisions（FK 方向：source→project；project.active_source_revision_id 为后置可空 FK，建表时以 deferred 或后建项目表外键的顺序处理，由 BASE-03-a 的建表脚本确定）。
- 哈希计算在服务端单次事务内完成并校验长度：`char_length` 用 UTF-16 code unit 计数（含代理对 2 单位）；canonical 转换与计数必须与附录 00 §7 的拒绝规则同源（共享 helper，SOURCE-02 卡交付，DB 模型调用它）。
- 读接口不触发任何创建/迁移/统计回填；`fragment_count` 等计算值按需实时查询。
