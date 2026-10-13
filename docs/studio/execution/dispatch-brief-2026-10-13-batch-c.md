# 批次 C 派发交接单 · 2026-10-13（波次 C1）

基线：分支 `studio-rebuild`，HEAD `e06939fa3015c551225220f9bd7ed9c23f8ce494`（批次 B 全部验收入账 + VIEW-01 WIP 入账后）。
执行器模型：显式 `provider=pation`、`model=qwen38-27b-w3a4`，**不传** `reasoning_effort`。派发前置自检见[批次 B 接续单](handoff-2026-10-13-batch-b.md) §0。

## 波次与并行依据

| 波次 | 卡 | 白名单 | 并行依据 |
| --- | --- | --- | --- |
| C1 | SCRIPT-02（SC6 采用剧本 revision 及影响） | `backend/studio/scripts/{service,api}.py`、`backend/studio/reviews/impact.py`、`tests/studio/test_scripts.py` | 与 REL/MEDIA 零重叠 |
| C1 | REL-02（OC 资产出现引用命令） | `backend/studio/relations/{service,api}.py`、`tests/studio/test_relations.py` | 零重叠 |
| C1 | MEDIA-01（私有媒体落盘与完整性） | `backend/studio/media/store.py`、`backend/infrastructure/**`、`tests/studio/test_media.py` | 零重叠 |

三卡文件集互不重叠，可并行；`tests/studio/conftest.py`、`backend/studio/router.py`、`backend/studio/*/models.py`、`backend/studio/contracts/**` 均为**只读**，任何一卡不得修改（需改路由挂载时报告主模型）。

主模型预接线（2026-10-13）：`backend/studio/router.py` 的「存在即挂载」清单原先缺 `relations.api`（REL-02 的路由会被静默丢弃），已由主模型补入；`scripts.api` 早已在清单内，SCRIPT-02 无需接线；MEDIA-01 无 HTTP 路由。

## 通用冻结约定（三卡共同）

- 鉴权 `core.auth.require_user`；错误一律走 `StudioAPIError`（`backend/studio/contracts/errors.py`）冻结形状 `{"error":{"code","message","details"}}`（附录 00 §4）。
- 写命令幂等：`(owner_id, command_id)` 命中 `CommandRecord` 且与首次 payload 一致 → 返回首次 `result_payload`（**HTTP 200**，覆盖 201）；异内容 → 422 `validation_failed` field=`command_id` rule=`reused`，零写为零 commit。放置位置照各卡 **幂等先于 CAS/状态检查** 的既定先例（`sources/fragment_service.py` F1/F4/F6/F7/F8、批次 B 裁定 5.1）。
- CAS 失败 → 409 `revision_conflict` details `{"object":{"kind","id"},"expected","actual"}`，真实值。
- 既有实现模式请直接读：`backend/studio/scripts/service.py`（SC1/SC4）、`backend/studio/assets/service.py`（AS1/AS4/AS10）、`backend/studio/sources/fragment_service.py`（F7/F8 preview+apply，**SCRIPT-02 的写序/错误形状以它为准**）。
- 测试沿用 `tests/studio/conftest.py` 的 `db`/`client` fixture（隔离数据根，勿指向 `data/studio`）。

---

## SCRIPT-02 · SC6 采用剧本 revision 及影响

来源：附录 03 §2 SC6 + 附录 00 §5 + 附录 07 RV1/RV5。**主模型补齐处已显式标注**。

### 1. `POST /api/studio/projects/{pid}/fragments/{fid}/script-adopt`（preview）

请求：`{target_fragment_id: str, command_id: uuid}`。
成功 200：`{preview_id, kind: "script_adopt", baseline, impact}`（**零业务副作用**；写 `ChangePreview` + `CommandRecord`）。

校验序（按序短路）：

1. 项目不存在/非属主 → 404 `not_found` details `{"kind":"project","id":pid}`；
2. 片段不存在/不属本项目 → 404 `not_found` details `{"kind":"fragment","id":fid}`；
3. `target_fragment_id != fid` → 422 `validation_failed` field=`target_fragment_id` rule=`mismatch`；
4. 片段 `state != "confirmed"` → 422 `precondition_failed` reason=`not_confirmed`，`blocked_by={"kind":"fragment","id":fid,"revision":<片段当前 revision>}`；
5. 片段 `source_revision_id != project.active_source_revision_id` → 422 reason=`source_revision_inactive`，`blocked_by` 同上；
6. 片段下无 `ScriptObject` → 422 reason=`no_objects`，`blocked_by` 同上（附录 SC6 preview：片段无对象）；
7. 片段 `state == "retired"` → 422 reason=`retired`，`blocked_by` 同上（**主模型补齐**：与 F5–F8 门禁惯例一致，retired 置末）；
8. 幂等（00 §3，先于写）：同 `(owner, command_id)` 且首次 payload 的 `target_fragment_id == fid` → 200 原 `result_payload`；异内容 → 422 `validation_failed` field=`command_id` rule=`reused`。

`baseline`（冻结、确定性；主模型按附录 03 文字落形）：**JSON 数组**——

```json
[{ "kind": "script_object", "id": "<oid>", "revision": <草稿 revision> }, ... ,
 { "kind": "script_adoption", "id": "<链尾 adoption id>", "revision": null }]
```

前半按对象 `seq` 升序（片段全部 `ScriptObject`）；若该片段已有采用（链尾），末尾追加一条 adoption ref；无采用则只有对象项。

`impact`：调用新增 `compute_script_adopt_impact(session, pid, fid)`（见 §3）。

`ChangePreview` 行：`kind="script_adopt"`、`payload=json({"target_fragment_id": fid})`、`baseline`/`impact` 按上冻结、`state="pending"`、`owner_id=user.id`、`project_id=pid`、`expires_at=created_at+1800`、`resolved_at=NULL`、`decision_id=NULL`。

### 2. `POST /api/studio/projects/{pid}/fragments/{fid}/script-adopt/apply`

请求：`{preview_id, command_id, expected_previous_adoption_id: str|null, expected_object_revisions: [{"object_id","revision","seq","kind"}...]}`。

成功 200（新 `ScriptAdoption` DTO，**主模型冻结**）：

```json
{ "object_ref": { "kind": "script_adoption", "id": "<新 id>", "revision": null },
  "fragment": { "kind": "fragment", "id": "fid", "revision": <片段当前 revision> },
  "object_revisions": [ { "object_id": "...", "revision": 2, "seq": 1, "kind": "dialogue" } ],
  "content_hash": "<64 hex>",
  "previous_adoption_id": "<链尾 id 或 null>",
  "created_at": <float> }
```

校验序：

1. 项目 404；2. 片段 404（形状同 preview 步骤 1/2）；
3. `preview` 不存在/跨项目 → 404 `not_found` details `{"kind":"preview","id":preview_id}`；`preview.kind != "script_adopt"` 或 payload 的 `target_fragment_id != fid` → 422 `validation_failed` field=`target_fragment_id` rule=`mismatch`；
4. 幂等（先于状态检查，F7/T43 惯例）：同 `(owner, command_id)` 命中且首次 payload `object_ref.kind=="script_adoption"`、`fragment.id==fid`、`previous_adoption_id==expected_previous_adoption_id` → 200 原 payload；异内容 → 422 `reused`；
5. preview 非 pending：`expired` 或（pending 且 `now > expires_at`）→ 409 `preview_stale` details `{"preview_id", "conflicts":[{"object":{"kind":"preview","id":preview_id},"expected":"pending","actual":"expired"}]}`，并惰性置 `state="expired"`；`applied`/`rejected`/`superseded` → 409 `preview_stale` 同形（actual=该 state，终态不再改写）；
6. CAS：`expected_previous_adoption_id` ≠ 当前链尾 id → 409 `revision_conflict` details `{"object":{"kind":"script_adoption","id":<链尾 id，无则 "">},"expected":<请求值>,"actual":<实际链尾 id 或 null>}`；
7. 片段门禁重校验（preview 步骤 4~7 全部；任一失配 → 409 `preview_stale`，conflicts 列出全部失配项）；
8. baseline 比对（写事务内重读为权威）：`expected_object_revisions` 与「当前片段全部对象按 `seq` 升序的快照」逐项全等（`object_id`/`revision`/`seq`/`kind`）；失配 → 409 `preview_stale{preview_id, conflicts:[全部失配项]}` 且 preview 置 `state="superseded"`、`resolved_at`；
9. 写（**单事务，无半次保存**；写序照 F7 apply）：`INSERT ScriptAdoption` → `INSERT ReviewDecision(decision="applied", preview_id, target_ref={"kind":"script_adoption","id":新id,"revision":null}, baseline_digest=sha256(规范化 preview.baseline))` → `UPDATE preview(state="applied", resolved_at, decision_id)` → `INSERT CommandRecord(result_payload=完整 200 体)`。

`content_hash` 冻结算法：对象按 `seq` 升序，逐个取 `f"{revision}:{sha256(text.encode('utf-8')).hexdigest()}"`，拼接后 `sha256(拼接串.encode('utf-8')).hexdigest()`。

**禁止项（逐条断言）**：不自动生成分镜（零 `Shot`/`ShotRevision`/`StudioJob`/`MediaArtifact` 写入）；不改任何已存在 `ScriptAdoption` 行；不改 `ScriptObject`/`ScriptRevision`；旧采用不被改写（R11）。

### 3. `backend/studio/reviews/impact.py` 新增 `compute_script_adopt_impact`

`compute_script_adopt_impact(session, pid, fid) -> {"affected": [...], "needs_review": [...], "preservable": [...]}`（沿用本文件既有 `_successor_group`/`_raw_rows`；各列表按 `object_ref.id` 升序；空表→空列表、**键不省略**；表未交付 → `OperationalError` 降级为空、绝不 500）：

- `affected[]`：`shot` —— `studio_shots.script_adoption_id` ∈ 该片段**后继组**的全部 adoption id；`{"object_ref":{"kind":"shot","id":<shot id>,"revision":<shot 当前 revision>}}`；
- `needs_review[]`：`job` —— `project_id=pid`、`status ∈ ("pending","running")` 且 payload 文本含后继组任一 fragment id 或该组任一 adoption id；`{"object_ref":{"kind":"job","id":job id,"revision":null},"reason":"frozen_input_references_previous_adoption"}`；
- `preservable[]`：`media_artifact`（`source_job_id ∈ 上一步 job 组`）→ 其 `edit_instance`（`media_id ∈ 上述 media`）→ 其 `release`（`poster_artifact_id ∈ 上述 media`）；三者 `object_ref.revision` 均 `null`。

不改本文件既有 `compute_impact` 行为（F7–F10 已验收）。

---

## REL-02 · OC 资产出现引用命令

来源：附录 06 §1/§2（OC1/OC2/OC3）。**只做出现族**；RL（来源关系）与 BD（绑定）不在本卡。

### 1. `POST /api/studio/projects/{pid}/relations/occurrences`（OC1）

请求：`{asset_id, source_fragment_id, range: {"start": int, "end": int} | null, command_id}`。

成功 201 DTO：

```json
{ "object_ref": { "kind": "asset_occurrence", "id": "<id>", "revision": <source_fragment_revision> },
  "asset":  { "kind": "asset",    "id": "<asset_id>",    "revision": <资产当前 revision> },
  "fragment": { "kind": "fragment", "id": "<fragment_id>", "revision": <片段当前 revision> },
  "range": { "start": 10, "end": 20 },
  "created_at": <float> }
```

> `object_ref.revision` 取 `source_fragment_revision`：与既有 `reviews/impact.py` 对 `asset_occurrence` 的发射保持一致（不新造第二套 revision 约定）。

校验序（按序短路）：

1. 项目 404（`kind=project`）；
2. 资产不存在/跨项目 → 404 `not_found` details `{"kind":"asset","id":asset_id}`；
3. 片段不存在/跨项目 → 404 `not_found` details `{"kind":"fragment","id":source_fragment_id}`；
4. `range` 形状与界（仅当非 null）：缺一/非整数 → 422 `range_invalid` details `{"rule":"non_integer"}`；`start >= end` → `{"rule":"empty"}`；`start < 片段 range_start` 或 `end > 片段 range_end` → `{"rule":"out_of_bounds"}`。**坐标系冻结**：与片段同一套 UTF-16 半开区间绝对偏移（不引入第二套坐标），范围必须落在该片段区间内；
5. 片段 `source_revision_id != project.active_source_revision_id` → 422 `precondition_failed` reason=`source_revision_inactive`，`blocked_by={"kind":"fragment","id":source_fragment_id,"revision":<片段当前 revision>}`（**不校验 confirmed**，与 SC4 先例一致）；
6. 幂等（00 §3）：同 `(owner, command_id)` 且首次 payload 等价（`asset_id`/`source_fragment_id`/`range` 全等）→ 200 原 payload（覆盖 201）；异内容 → 422 `reused`；
7. 唯一冲突：已存在同（`asset_id`,`source_fragment_id`,coalesce(range_start),coalesce(range_end)）行 → 409 `duplicate` details `{"scope_ref": "<字符串>"}`（**主模型实现时修正**：对齐既有 `StudioAPIError.duplicate(scope_ref: str)` 签名与 projects 域先例，`scope_ref` 为 `asset_occurrence:<asset_id>:<fragment_id>:<start|->:<end|->` 形式的字符串，不用 dict）；
8. 写（单事务）：`INSERT AssetOccurrence(asset_revision=资产当前 revision, source_fragment_revision=片段当前 revision, ...)` + `INSERT CommandRecord`。

**禁止**：不改 `Asset`/`AssetRevision`；不建 `Fragment`/`FragmentRevision`/`ScriptObject`；不写 `AssetBinding`。

### 2. `GET /api/studio/projects/{pid}/relations/occurrences?asset_id=&source_fragment_id=&cursor=`（OC2）

200 `{"items":[DTO...],"next_cursor": <id|null>}`：过滤项均可选、只返回本项目的行；排序 `id DESC`（**主模型实现时修正**：原写 asc，按 P2/P1 已冻结的域倒序惯例与 `list_projects`/`list_fragments` 对齐）；`cursor` 非 `^[0-9a-z]{26}$` → 422 `validation_failed` field=`cursor` rule=`format`（同域惯例）；`limit` 缺省 50、clamp [1,200]；`items[]` 用与 OC1 相同的 DTO。

### 3. `DELETE /api/studio/projects/{pid}/relations/occurrences/{occurrence_id}`（OC3）

请求体（DELETE 带 body）：`{command_id}`。

- 成功 200：`{"object_ref": {"kind":"asset_occurrence","id":occurrence_id,"revision":<source_fragment_revision>}, "deleted": true}`，单事务删该行 + `CommandRecord`；
- 行不存在/跨项目 → 404 `not_found` details `{"kind":"asset_occurrence","id":occurrence_id}`；
- 同 `command_id` 重放 → 200 原 payload（00 §3）；删掉后**换新 command_id** 再来 → 404（附录 06 注：客户端以 404 收敛）；
- 无 preview、零其他业务副作用。

---

## MEDIA-01 · 私有媒体落盘与完整性

来源：附录 08 §4/§7 + 契约 §6。**无 HTTP 路由**，交付纯逻辑 + 测试。旧后端 `backend/video_files.py`、`video_storage.py` 等**不得 import**（R01/R11）。

> 白名单补齐（主模型裁定）：新建 `backend/infrastructure/__init__.py`、`backend/infrastructure/media/__init__.py`（此前不存在该包；同卡内允许）。

### 1. `backend/infrastructure/media/probe.py`（纯工具，只依赖 stdlib + `imageio_ffmpeg`）

- `class MediaProbeError(Exception)`；
- `ffmpeg_binary() -> str`：`imageio_ffmpeg.get_ffmpeg_exe()`；
- `probe_media(path) -> dict`：`subprocess.run([ffmpeg_binary(), "-i", str(path)], capture_output=True, text=True, timeout=30)`，解析 stderr（静态构建无 ffprobe）；返回
  ```python
  {"duration_ms": int|None, "width": int|None, "height": int|None,
   "has_video": bool, "has_audio": bool}
  ```
  文件不可读/无法解析/非媒体 → `raise MediaProbeError`（**绝不返回伪数据**）。

### 2. `backend/studio/media/store.py`

常量（主模型冻结；附录只给格式清单，未给上限，补白处标注）：

- `ALLOWED_EXTENSIONS = ("mp4", "webm", "mov", "m4a", "wav", "mp3")`（附录 08 §4 接受输入）；
- `MAX_MEDIA_BYTES = 512 * 1024 * 1024`（**主模型补白**：单文件上限 512 MiB；测试用 monkeypatch 调小，不写真实大文件）。

函数：

- `media_root() -> Path`：`backend/core/db.py` 的 `DATA_DIR / "media"`（ROOT-01 冻结数据根之下）；
- `public_root() -> Path`：`DATA_DIR / "public"`（仅供分离性断言，**永不写入**）；
- `safe_filename(filename) -> str`：空、绝对路径、含 `/`、`\`、`..`、NUL → 422 `validation_failed` field=`filename` rule=`invalid`；返回 basename；
- `store_media(session, *, project_id, filename, data: bytes, media_kind, source, source_job_id=None) -> MediaArtifact`：
  1. 扩展名不在白名单 → 422 `validation_failed` field=`filename` rule=`unsupported_type`（message 含扩展名）；
  2. `data` 空 → 422 field=`size` rule=`empty`；`len(data) > MAX_MEDIA_BYTES` → 422 field=`size` rule=`too_large`；`media_kind ∉ {image,video,audio}` → 422 field=`media_kind` rule=`invalid`；`source ∉ {upload,asset_generate,shot_generate,vendor_normalized,export}` → 422 field=`source` rule=`invalid`；
  3. `artifact_id = make_ulid()`；相对名 `rel = f"{project_id}/{artifact_id}.{ext}"`；`dest = media_root()/rel`；父目录 `mkdir(parents=True, exist_ok=True)`；
  4. **原子落盘**：同目录临时文件（`dest.parent / f".{artifact_id}.tmp"`）写入 → `flush` → `os.fsync` → `os.replace(tmp, dest)`；失败 unlink 临时文件并 re-raise；
  5. `sha256 = hashlib.sha256(data).hexdigest()`；`media_kind in ("video","audio")` → `probe_media(dest)` 取 `duration_ms/width/height`，probe 抛错则 unlink `dest` 并 re-raise（**不落行**）；`image` → 三者 `None`；
  6. `session.add(MediaArtifact(id=artifact_id, project_id=..., media_kind=..., filename=rel, size_bytes=len(data), sha256=..., duration_ms=..., width=..., height=..., source=..., source_job_id=..., created_at=time.time()))` + `session.flush()`，**返回该行、不 commit**（事务由调用方掌控）；
  7. 步骤 4/6 任一异常：删除已写文件、不留下任何行（**写失败无伪就绪记录**）；
- `artifact_path(artifact) -> Path`：`(media_root()/artifact.filename).resolve()`，必须 `is_relative_to(media_root().resolve())`，否则 `raise ValueError`（越界路径拒绝）；
- `verify_integrity(artifact) -> bool`：文件存在 ∧ `stat().st_size == size_bytes` ∧ 重算 sha256 相等。

**禁止**：写入或创建 `data/studio/public/**`（私有/公开物理分离）；import 旧后端媒体模块；把文件挂到任何公开静态目录。

### 3. `tests/studio/test_media.py`（新建）

覆盖：未知扩展名/路径穿越/空数据/超限/非法 `media_kind`/非法 `source` 各 422 且零落盘零落行；真实小视频（用 ffmpeg 生成或用 PoC fixture 生成器）落盘后 `verify_integrity` 为真、`filename` 为 `{pid}/<ulid>.<ext>` 相对名、sha256/大小与字节一致；篡改文件后 `verify_integrity` 为假；损坏文件 `probe_media` 抛 `MediaProbeError` 且无 `MediaArtifact` 行、无残留文件；monkeypatch `os.replace` 抛错 → 无行无文件；`artifact_path` 对人为越界 `filename` 抛 `ValueError`；断言存储路径不在 `public_root()` 之下。

---

## 验收与入账（不变）

执行器只提交，**不得 `git add/commit`**；主模型审 diff + 真跑本卡测试 + 独立复验 → 单卡提交 → 账本 `acceptance`（`tested_baseline`=该卡提交）→ 整批收尾全量 `pytest tests/studio`。

## 后续波次（C2）

| 卡 | 状态 | 说明 |
| --- | --- | --- |
| VIEW-01 | **已冻结，见下** | 白名单三文件；WIP 草案已在 `e06939f` 入账 |
| CHAT-01 | **已冻结，见下** | 附录 10 只给表与语义、无命令表；命令面由主模型冻结（五路由） |
| EDIT-01 | **已冻结，见下** | 附录 08 §2/11 §2 只给语义；读写路由与 manifest 校验由主模型冻结 |
| RETIRE-01 | 待冻结 | 需主模型先按实施 HEAD 出逐文件退役清单（ROOT-05 盘点不能代替） |

### VIEW-01 · 新项目工作台状态容器（C2 冻结）

基线草案：`web/features/studio/shell/studio-state.ts`（`e06939f`，243 行，未接线未验收）。白名单：`web/features/studio/shell/studio-state.ts`、`web/features/studio/StudioApp.tsx`、`web/tests/studio/state.test.cjs`。

**冻结导出**（后续 VIEW-03/04 与 `state.test.cjs` 依赖，不得改名/改形）：`ObjectRef`、`ProjectDtoSummary`、`StudioView`、`SourceSwitchMode`、`JobStatus`、`JobFeedEntry`、`StudioState`、`StudioAction`、`DEFAULT_VIEW`（`"source"`）、`DEFAULT_SOURCE_SWITCH_MODE`（`"reader"`）、`createInitialState()`、`normalizeSelectionRef(ref)`、`studioReducer(state, action)`。

**语义冻结**：

- 项目隔离：`setProject` / `switchProject` / `hydrate` 一律把 `selection`/`view`/`jobFeed` 重置到新项目初态（旧项目状态不残留）；`switchProject`/`hydrate` 载入 `project` DTO 摘要并把 `sourceSwitch.activeSourceRevisionId` 设为 `project.active_source_revision_id`；`hydrate` 用服务端 DTO 重建（含 `selectionRef`），**不读 localStorage**；
- 后台 job：`pushJob`/`updateJob`/`clearJobFeed` 只改 `jobFeed`，**永不修改 `view`**（job 不跳标签）；`pushJob` 保留最近 10 条（`JOB_FEED_CAP`）；
- `setSelection` 经 `normalizeSelectionRef` 归一：空 `kind`/空 `id`/`revision` 非正整数 → `null`；
- 未知 action → 返回**原引用**（引用相等），不抛错、不新建对象；
- 无旧 stage/context 兼容分支（R11）。

**StudioApp.tsx 接线**：`useReducer(studioReducer, undefined, createInitialState)`；`projectId` prop 变化 → dispatch `{type:"setProject", projectId}`（`invalid` 或 `projectId===null` 时传 `null`）；保持既有 DOM 结构与 `<style jsx>`，以及 invalid / 未选择 / 空态三分支文案。**约束**：`web/tests/studio/studio-app.test.cjs` 断言 StudioApp.tsx 不得含 `"/api/`、不得出现 `fetch(`、不得 import `app/`、不得引用 `ProjectHome|TaskActivity|reader` —— 本卡**不做网络水合**（项目 DTO 由后续卡经 `hydrate` 注入），不得新增 API 客户端文件（白名单外）。

**测试**：`web/tests/studio/state.test.cjs` 新建，照 `selection.test.cjs` 的 `vm` + `typescript.transpileModule`（CommonJS）模式加载 `shell/studio-state.ts`，断言：初态；项目隔离（切项目后 selection/view/jobFeed 归初态）；`switchProject`/`hydrate` 载入 DTO 与 active source revision；`normalizeSelectionRef` 四类非法输入归 `null`；`pushJob`/`updateJob` 不改 `view`、cap=10；`clearJobFeed`；未知 action 引用不变。

**验收**：`node --test 'web/tests/studio/*.test.cjs'`（既有 385 例不回归）与 `web/node_modules/.bin/tsc --noEmit -p web/tsconfig.json`（exit 0）。

### CHAT-01 · 会话和消息 API（C2 冻结）

来源：附录 10 §1–§3 + 附录 00 §3/§4/§6。白名单：`backend/studio/conversations/{service,api}.py`、`tests/studio/test_conversations.py`。**本卡不调 LLM、不建 Proposal**（提案由 CHAT-02 处理器产生；「回答本身不保存业务」R10）。

两个冻结 DTO：

```json
ConversationDto = { "object_ref": {"kind":"conversation","id":..,"revision":null},
  "scope_kind": "fragment", "scope_id": "..", "title": "", "created_at": <float>, "updated_at": <float> }
MessageDto = { "object_ref": {"kind":"message","id":..,"revision":null},
  "conversation_id": "..", "sender": "user|assistant", "content": "..",
  "refs": [{"kind":"fragment","id":"..","label":".."}] | null,
  "job_id": null, "created_at": <float> }
```

路由（均 `/api/studio/projects/{pid}/...`，属主隔离：会话以 `owner_id` 限定，他人会话一律 404 不泄漏）：

1. `GET /conversations?scope_kind=&scope_id=&cursor=` → 200 `{"items":[ConversationDto],"next_cursor":<id|null>}`；只返回 `owner_id==user.id` 的行；过滤项可选；按 `id` 升序；默认 50、上限 200。
2. `POST /conversations`（**取或建**）请求 `{scope_kind, scope_id, command_id}`：项目 404 → `scope_kind ∉ {project,fragment,scene,shot,asset,edit}` → 422 `validation_failed` field=`scope_kind` rule=`enum` → `scope_kind=="project"` 时 `scope_id` 必须 `== pid`（否则 422 field=`scope_id` rule=`mismatch`），其余 kind 的 `scope_id` 必须存在且属本项目（fragment→`studio_fragments`、scene→`studio_scenes`、shot→`studio_shots`、asset→`studio_assets`、edit→`studio_edit_drafts.edit_id`）否则 404 `not_found` details `{"kind":scope_kind,"id":scope_id}` → 幂等 → **已存在 (project,scope_kind,scope_id,owner) 行 → 200 返回该行 DTO（取或建，不报 duplicate）**；否则 201 新建并返回 DTO。
3. `GET /conversations/{cid}` → 200 `{...ConversationDto, "messages":[MessageDto × 最近 50 条按 id 升序]}`；不存在/跨项目/非本 owner → 404 `not_found` details `{"kind":"conversation","id":cid}`。
4. `GET /conversations/{cid}/messages?cursor=&limit=` → 200 `{"items":[MessageDto],"next_cursor":<id|null>}`，按 `id` 升序，默认 50、上限 200。
5. `POST /conversations/{cid}/messages` 请求 `{sender, content, refs?, job_id?, command_id}` → 201 `MessageDto`。校验序：项目 404 → 会话 404（同形状）→ `sender ∉ {user,assistant}` → 422 field=`sender` rule=`enum` → `content.strip()` 为空 → 422 field=`content` rule=`required`；长度 > 8000 → rule=`too_long`（**主模型补白**）→ `refs` 非 null 时必须是数组且每项 `{kind:非空 str, id:非空 str, label:str}`，否则 422 field=`refs` rule=`invalid`（**主模型补白**）→ `job_id` 非 null 必须存在且属本项目，否则 404 `not_found` details `{"kind":"job","id":job_id}` → 幂等 → 单事务 `INSERT Message` + `UPDATE conversation.updated_at` + `CommandRecord`。`refs` 入库为 JSON 文本，出参解析为数组（`null → null`）。

**禁止**：不写业务表（Fragment/Scene/Shot/Asset/Edit/Job）；不建/改 `Proposal`；不调 LLM、不发网络请求；`title` 本卡固定空串（改名命令属后续卡）。

### EDIT-01 · 项目剪辑草稿读写（C2 冻结）

来源：附录 08 §2（EditManifest v1）/§3（支持范围）、附录 11 §2。白名单：`backend/studio/edits/{service,api}.py`、`tests/studio/test_edits.py`。**本卡不做确认（EDIT-08）与导出**，只做草稿读写。

路由：

1. `GET /api/studio/projects/{pid}/edit` → 200

```json
{ "edit_id": <str|null>, "edit_revision": <int|null>, "cas_revision": <int|null>,
  "manifest": <EditManifest v1 object|null>, "manifest_digest": <64hex|null>,
  "instances": [InstanceDto...],
  "confirmed": { "object_ref": {"kind":"confirmed_edit","id":..,"revision":<edit_revision>},
                 "manifest_digest": "..", "output_spec": {...} } | null }
```

项目无任何草稿行 → `edit_id/edit_revision/cas_revision/manifest/manifest_digest` 全 `null`、`instances: []`、`confirmed: null` —— **空态就是空态，不造样例实例、不造 96 秒假时长**。`instances[]` 来自当前草稿行的 `studio_edit_instances`；`confirmed` 取该 `edit_id` 的 `max(edit_revision)` 确认行。

2. `POST /api/studio/projects/{pid}/edit/draft` 请求 `{edit_id: str|null, manifest, expected_revision: int|null, command_id}` → 200 DraftDto：

```json
{ "object_ref": {"kind":"edit","id":<edit_id>,"revision":<edit_revision>},
  "edit_revision": 2, "cas_revision": 2, "manifest": {...},
  "manifest_digest": "<64hex>",
  "instances": [ { "object_ref": {"kind":"edit_instance","id":..,"revision":null},
                   "instance_id": "..", "media_id": "..", "source_in_ms": 0,
                   "source_out_ms": 1000, "speed": 1.0,
                   "timeline_start_ms": 0, "track_id": 0 } ],
  "updated_at": <float> }
```

- 校验序：项目 404 → `edit_id` 非 null 时草稿必须存在且属本项目（否则 404 `not_found` details `{"kind":"edit","id":edit_id}`，跨项目同形状）→ manifest 校验（**全部 violations 一次报**，422 `validation_failed`）→ 幂等 → CAS。
- manifest 校验（**主模型冻结**，`field` 用带下标的精确路径）：
  - `manifest` 非 object、或 `manifest.schema != "storyloom.editmanifest/v1"` → field=`manifest.schema` rule=`invalid`；
  - `canvas.width`/`canvas.height`/`canvas.fps` 非正整数 → field=`manifest.canvas.<k>` rule=`invalid`；
  - `instances` 非数组 → field=`manifest.instances` rule=`invalid`；逐项：`id`/`media_id` 非空字符串（rule=`required`）；`track_id == 0`（v1 单轨，rule=`invalid`）；`timeline_start_ms`/`source_in_ms` 非负整数（rule=`invalid`）；`source_out_ms > source_in_ms`（rule=`invalid`）；`0.25 ≤ speed ≤ 4.0`（rule=`invalid`）；
  - `media_id` 必须存在且属本项目 `studio_media_artifacts`（rule=`not_found`）；
  - `manifest_digest` 由服务端计算 = `sha256(json.dumps(manifest, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")).hexdigest()`，忽略客户端传入值。
- 新建（`edit_id` 为 null）时 `expected_revision` 必须为 null，否则 422 field=`expected_revision` rule=`invalid`；已有草稿时 `expected_revision` 必须 `== 当前 cas_revision`，否则 409 `revision_conflict` details `{"object":{"kind":"edit","id":edit_id},"expected","actual"}`。
- 写（单事务，无半次保存）：`INSERT EditDraft(edit_id, edit_revision=prev+1, manifest, manifest_digest, cas_revision=prev+1, updated_at)` + 为新草稿行 `INSERT EditInstance`（`id=make_ulid()`、`instance_id=manifest.instances[i].id`、`draft_id=新行 id`）+ `CommandRecord`。首次保存 `edit_id=make_ulid()`、`edit_revision=1`、`cas_revision=1`。
- 幂等：同 `(owner, command_id)` 同 payload → 200 原 payload、零新行；异内容 → 422 `reused`。
- **禁止**：不写 `ConfirmedEdit`/`Release`；不改 `MediaArtifact`；不删旧草稿行（版本链只追加）；不引入第二套时间单位（一律整数毫秒）。

两者（CHAT-01/EDIT-01）派发前仍需核对：`backend/studio/router.py` 的「存在即挂载」清单已含 `conversations.api` 与 `edits.api`，无需接线改动。

## 执行结果（2026-10-13 晚）

| 卡 | 结果 |
| --- | --- |
| REL-02 | **已验收**，提交 `3ed8b58`（12 passed；独立复验 29/29）。由**主模型亲自实现**：两轮执行器均因 64k 上下文超限零产出失败。 |
| SCRIPT-02 | 执行器失败（同因，零产出），待重派或主模型实现 |
| MEDIA-01 | 执行器失败（同因，零产出），待重派或主模型实现 |
| VIEW-01 | 执行器失败（同因，零产出，失败前已完成基线核对），待重派或主模型实现 |

**根因（实测，已写入 `observed_baselines.executor_context_limit_2026_10_13` 与 [执行器提示词](executor-prompt.md)）**：本地端点 `pation/qwen38-27b-w3a4` 的 contextWindow = 65536；五次派发全部在累计 tool 结果 117820~154679 字节时 `400 "Input plus output exceeds 65536 tokens for this model"` 失败且零产出（432 字节的探测会话正常）。批次 A 能通过是因为当时端点还有更大上下文的模型变体。

**重新派发的硬约束**：单卡「阅读量 + 产出量」控制在约 50KB 以内；派发单只给精确行区间/函数名，禁止整读 >400 行文件；超出该预算的卡由主模型实现或先拆成更小的叶卡。
