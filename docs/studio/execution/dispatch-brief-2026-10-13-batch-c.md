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
7. 唯一冲突：已存在同（`asset_id`,`source_fragment_id`,coalesce(range_start),coalesce(range_end)）行 → 409 `duplicate` details `{"scope_ref":{"kind":"asset_occurrence","asset_id":...,"source_fragment_id":...,"range_start":...,"range_end":...}}`；
8. 写（单事务）：`INSERT AssetOccurrence(asset_revision=资产当前 revision, source_fragment_revision=片段当前 revision, ...)` + `INSERT CommandRecord`。

**禁止**：不改 `Asset`/`AssetRevision`；不建 `Fragment`/`FragmentRevision`/`ScriptObject`；不写 `AssetBinding`。

### 2. `GET /api/studio/projects/{pid}/relations/occurrences?asset_id=&source_fragment_id=&cursor=`（OC2）

200 `{"items":[DTO...],"next_cursor": <id|null>}`：过滤项均可选、只返回本项目的行；按 `id` 升序（ULID 时间有序），默认页 50、上限 200（附录 00 §6）；`items[]` 用与 OC1 相同的 DTO。

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

## 后续波次（C2，待冻结后派发）

`CHAT-01`、`EDIT-01`、`VIEW-01`（WIP `studio-state.ts` 已在 `e06939f` 入账）、`RETIRE-01`（需主模型先出逐文件退役清单）。四者接口面在附录 10 / 附录 08+11 / 03-任务卡之外仍需主模型冻结命令表，故不在 C1 同批派发。
