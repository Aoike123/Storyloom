# 附录 11 · 媒体、确认剪辑与发布交付（ROOT-04）

版本：2.2 · ROOT-04 冻结 · 2026-10-04。覆盖：媒体/剪辑/发布表（DB-10，schema 以附录 08 为基线）、Release 与版本共存、公开访问白名单、poster 与项目封面（01 附录承诺的发布侧）、观看页数据。依据：[产品规范 R09/R12](../../product/product-spec.md)、[领域契约 §6](../domain-contracts.md)、附录 08（内核/存储/格式）。

实施位置：`backend/studio/media/`、`backend/studio/edits/`、`backend/studio/publishing/`；前端 `web/features/studio/film/`、`web/features/studio/releases/`、公开 `/watch/[releaseId]`（app 路由，只读公开数据）。

## 1. 表（DB-10）

```text
studio_media_artifacts
  id            PK ULID
  project_id    FK studio_projects
  media_kind    TEXT   -- image | video | audio
  filename      TEXT   -- 私有存储内相对名（附录08 §4：data/studio/media/{pid}/）
  size_bytes    INT
  sha256        TEXT
  duration_ms   INT nullable    -- 视频/音频
  width/height  INT nullable
  source        TEXT   -- upload | asset_generate | shot_generate | vendor_normalized | export
  source_job_id ULID nullable
  created_at

studio_edit_drafts
  id            PK ULID
  project_id    FK studio_projects
  edit_id       ULID         -- 稳定剪辑对象 id（草稿与确认共享）
  edit_revision INT          -- 每次保存 +1
  manifest      JSON         -- EditManifest v1（附录08 §2）
  manifest_digest TEXT
  cas_revision  INT          -- 写 CAS
  updated_at
  UNIQUE (edit_id, edit_revision)

studio_edit_instances          -- 草稿内实例的显式行（供 UI 单独编辑/删除引用）
  id            PK ULID
  draft_id      FK studio_edit_drafts
  instance_id   ULID         -- = manifest 内 instance id
  media_id      FK studio_media_artifacts
  source_in_ms / source_out_ms INT
  speed         REAL
  timeline_start_ms INT
  track_id      INT default 0
  UNIQUE (draft_id, instance_id)

studio_confirmed_edits
  id            PK ULID
  edit_id       ULID
  edit_revision INT          -- 确认时冻结的草稿版本
  manifest      JSON         -- 冻结 manifest 原样
  manifest_digest TEXT
  output_spec   JSON
  created_at
  UNIQUE (edit_id, edit_revision)

studio_releases
  id            PK ULID
  project_id    FK studio_projects
  name          TEXT
  confirmed_edit_id FK studio_confirmed_edits
  poster_artifact_id FK studio_media_artifacts nullable
  status        TEXT   -- draft | published | retired
  predecessor_release_id ULID nullable   -- 同名版本链
  public_dir    TEXT nullable            -- public/{releaseId}/ 相对路径
  published_at  TIMESTAMPTZ nullable
  created_at / updated_at
  cas_revision  INT
  UNIQUE (project_id, name) -- v2.4 修正：条件唯一索引，仅 status IN ('draft','published') 生效
                             -- （plain UQ 与 R12 同名版本链矛盾：再发布=新 published 行+旧行退役保留，
                             --   两行同名必然并存；条件唯一 = 每 name 至多一个存活行，
                             --   即创建时 duplicate_scope 语义，退役链行可共存）
```

## 2. 剪辑与确认（R09）

- **草稿编辑**：`edit_id` 长期稳定；每次“保存”= 新 edit_revision（manifest 快照 + digest），写走 CAS（`expected_revision`）；实例增删/拖动 → 改草稿 manifest 再保存（R09：明确插入才写实例；拖动未保存=纯本地）。
- **确认**（EDIT-04，preview 必须，kind=`edit_confirm`）：preview 展示冻结后的 manifest（实例数/总时长/每实例来源区间）→ apply 建 `studio_confirmed_edits`（引用当前草稿 edit_revision 的 manifest 原样）。之后草稿继续可改，**不影响已确认剪辑**。
- **导出**（EDIT-05/06）：`export` job（09 §3）输入 = confirmed_edit_id + manifest_digest；同 ConfirmedEdit 重新提交 = **同版重试**（R09）；成片落 MediaArtifact(source=export)。
- 旧 `EditInstance` 契约字段全部映射到 `studio_edit_instances` + manifest（契约 §6 形状不变）。

## 3. 发布与版本共存（R12）

- **创建发布**：选 confirmed_edit + 名称（项目名或自定义）；已存在**存活**（draft/published）同名行 → 422 `duplicate_scope`（v2.4 条件唯一）。
- **发布**（RV5，preview 必须，kind=`release_publish`）：
  - preview impact = 将复制到公开目录的文件清单（成片 mp4 + poster 图，逐文件 sha256）与旧同名版本处置（被 `predecessor_release_id` 取代，仍保留可看）。
  - apply（`release_publish` job）：复制冻结产物到 `data/studio/public/{releaseId}/`（原子写；目录只含本次发布白名单文件）→ Release=published + public_dir。
- **新旧版本共存**：同名再发布 = 新 release 行（predecessor 指向旧行）；**旧版本不删除**，public 目录各自保留，观看页均可播放（R12）。
- **退役**：`retire` 命令（非 preview）→ status=retired：从“已发布列表”移除，公开文件保留可访问（R12：已发布可退役，公开访问仍成立）；无自动文件删除。
- **poster**：Release.poster_artifact_id（图片 MediaArtifact）；**项目封面 = 该项目最新 published release 的 poster**（附录 01 冻结的封面来源即此，无项目封面字段）。

## 4. 公开访问（安全边界）

- 观看页 `/watch/[releaseId]`：只读 `studio_releases`（status∈{published,retired}）+ 公开目录静态服务（白名单文件、无目录列举、https）；**私有原文/过程媒体/草稿永不暴露**（契约 §6 验收）。
- 公开列表页：只列 published 且未退役的 release（project 级公开性由 R12 项目公开开关控制，开关在 `studio_projects.visibility`，附录 01 已冻结）。
- 公开视频流：mp4 + `<video>` 原生播放（附录 08 预览路径同构），无转码服务。

## 5. 与 00 附录 preview kind 的收尾

07 附录预留的 `edit_confirm`/`release_publish` 两种 preview 的 payload/impact 定义：

| kind | baseline | impact.affected | impact.needs_review | impact.preservable |
| --- | --- | --- | --- | --- |
| `edit_confirm` | {edit_id, edit_revision, manifest_digest} | 确认剪辑行（将冻结） | 草稿后续修改 | 导出/发布入口不变 |
| `release_publish` | {release_id, name, public_dir} | 公开目录文件清单（含 sha256）、旧同名版本被取代 | 观看页可访问性 | 旧版本继续可播 |
