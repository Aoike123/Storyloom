# 批次 B 接续单 · 2026-10-13（在新 task 中执行）

本文件是给**新开的顶层 task** 的接续单。当前会话的子代理白名单已在创建时固化，无法再用 pation；新 task 会重新采样 profile。

## 0 会话与模型前置（先读，否则派发必失败）

- 执行器路线：显式 `provider=pation`、`model=qwen38-27b-w3a4`，**不传** `reasoning_effort`。
  本地端点 `http://127.0.0.1:18982/v1` 当前只服务这一个模型（contextWindow 65536，默认 effort=medium）。`-64k`/`-192k`/`-256k`/`-128k` 后缀 id 均已不存在。
- 白名单机制：Harness 只在**全新顶层会话**（`firstLiveSeq===0` 且无 `session/end-seed`）创建时调用 `settings.current()` 采样一次并写入会话投影；此后永久优先、不重算（`packages/subagent/tool-subagent/src/index.ts:399-426`）。改 profile 后必须开新 task；**重启服务救不了旧会话**。
- 新 profile 已删除 `tool-subagent.agentOptions` 里硬编码的子代理路线，不显式传 provider/model 时子代理继承父会话模型。并发上限 `maxActiveSubagents: 6`。
- 派发前自检：先 `list_subagent_models`（无参）应列出 `pation`；再试着派一个只回 `PROBE_OK` 的探测子 agent。被拒就不要继续派发。

## 1 状态快照

| 项 | 值 |
| --- | --- |
| 实施目录 | `/mnt/Code/agent-orchestration/workflow/Storyloom` |
| 分支 / HEAD | `studio-rebuild` / `feee816`（领先 origin 21） |
| 账本 | 叶卡 54/172 accepted；计划 v2.2、契约 v2.9 |
| 后端 | `.venv/bin/python` (3.12.3)；`pytest tests/studio` → 448 passed（约 240s） |
| 前端 | `node --test web/tests/studio/*.test.cjs` → 385 passed；`web/node_modules/.bin/tsc --noEmit -p web/tsconfig.json` → exit 0 |
| 服务端口 | 3011 / 3021 **未启动**（需显式给 `STUDIO_DATA_DIR`，见账本 `implementation.startup`） |

**工作树里有两张卡的实现 + 测试，已实现未验收、未提交，切勿 checkout/clean 丢掉：**

| 文件 | 增量 | 归属 |
| --- | --- | --- |
| `backend/studio/scripts/service.py` | +324 | SCRIPT-01-b |
| `backend/studio/scripts/api.py` | +45 | SCRIPT-01-b |
| `tests/studio/test_scripts.py` | +464（新增 t17~t37，共 21 例） | SCRIPT-01-b |
| `backend/studio/assets/service.py` | +234 | ASSET-01-d |
| `backend/studio/assets/api.py` | +52 | ASSET-01-d |
| `tests/studio/test_assets.py` | +336（新增 t32~t39，共 8 例） | ASSET-01-d |
| `web/features/studio/shell/studio-state.ts`（**未跟踪**） | 243 行 | VIEW-01 半成品，归属待裁定 |

两卡文件集互不重叠，可**分别单卡提交**（`git add` 精确到路径，不要 `git add -A`）。

## 2 立即执行队列

- [ ] **T1 裁定三处接口偏差**（见 §5，已给建议结论，落成文字即可）
- [ ] **T2 SCRIPT-01-b 验收**：审 diff → 跑本卡测试 → 独立复验 → 单卡提交 → 账本入账
- [ ] **T3 修 01-d 复验脚本后验收 ASSET-01-d**：脚本在造数据阶段撞 FK（见 §5.5），必须先修
- [ ] **T4 批次收尾**：全量 `pytest tests/studio` + 前端两件套，确认无回归
- [ ] **T5 家族验收**：`SCRIPT-01`、`ASSET-01` 两族父卡随子卡全通过收口
- [ ] **T6 裁定 VIEW-01 遗留** `studio-state.ts`（补接线+测试后验收，或丢弃重派）
- [ ] **T7 派发下一波**（§6，含依赖就绪的 9 张）

## 3 SCRIPT-01-b（SC4 修改剧本草稿）验收清单

接口冻结（[批次 B 交接单](dispatch-brief-2026-10-13-batch-b.md) §「SCRIPT-01-b」）：
`POST /api/studio/projects/{pid}/script-objects/{oid}/revise`，请求 `text?`/`speaker?`/`adaptation_note?`/`expected_revision`(int 必填)/`command_id`。

- [ ] 缺席≠null：缺席=保持原值；`speaker`/`adaptation_note` 显式 null=清空；`text` 显式 null 视同缺席（t18~t21）
- [ ] 三者全缺席 → 422 `validation_failed` field=`payload` rule=`empty`（t23/t24）
- [ ] 字段校验：text 空白 (`blank`)、speaker>80 / adaptation_note>300 (`too_long`)、expected_revision 非正整数 (`positive`)，多错一次全报（t25~t28）
- [ ] 校验序：项目 404 → 对象 404（不存在/跨项目同形状）→ 对象 retired 422 → 字段校验 → 片段 `source_revision_inactive`/`retired`（**不校验 confirmed**）（t29~t33）
- [ ] CAS：`expected_revision` ≠ 当前草稿 revision → 409 `revision_conflict`，`details.object.expected/actual` 为真实值（t22）
- [ ] 幂等：同 command_id 同内容 → 200 且逐字段等于首次 payload；不同内容 → 422 field=`command_id` rule=`reused`；两者均零写入（t34/t35/t37）
- [ ] 写入：新 `ScriptRevision`(revision+1、缺席取现值、`source_fragment_revision`=保存时片段 revision) + 对象行 `current_revision_id`/`speaker` 镜像/`updated_at` + `CommandRecord` 单事务（t36 直查库）
- [ ] **零采用写入**：`studio_script_adoptions` 恒 0 行（t36 直查库断言）
- [ ] 成功体与 SC1 DTO 同形，`object_ref.revision` = 新值，仅 8 键
- [ ] 回归：`test_scripts.py` 37 passed（含 SCRIPT-01-a 的 t1~t16 无回归）
- [ ] 独立复验：**需新建** `data/scratch/verify_script_01b.py`（`verify_script_01a.py` 可作模板：独立数据根 + TestClient + 直查 SQLite，`tests/` 之外的 17 项做法见 SCRIPT-01-a 账本条目）

## 4 ASSET-01-d（AS4 修改资产规格）验收清单

接口冻结 `POST /api/studio/projects/{pid}/assets/{aid}/spec`，请求 `spec`/`expected_revision`/`command_id`。

- [ ] 200 且新 revision = 当前+1，自动成为当前（`current_revision_id`/`updated_at`）
- [ ] **继承**上一 revision 的 `media_artifact_id` 与 `generation_input`（本阶段恒 NULL，但继承语义要在有真实 media 行时可观测）
- [ ] `review_status` 回到 `unreviewed`；旧 revision 行**不可变**
- [ ] spec 按资产 type 校验（复用 `_validate_spec`，含 ASSET-01-c 的 scene `time_of_day`/`weather` 可选键）
- [ ] `name`/`type`/`character_id` 不可经此命令修改（t 覆盖 + 探针结论见 §5.4）
- [ ] 409 `revision_conflict`；404 项目/资产（跨项目同形状）
- [ ] **零采用/绑定写入**：`studio_asset_occurrences`/`studio_asset_bindings` 恒 0 行（查库断言）
- [ ] DTO 与 §2 详情读同形，`versions[]` 含**全部**并存版本（revision 升序）；条目键集与 AS1 创建 DTO 一致 = `{created_at, media_artifact_id, review_status, revision, spec}`（已探针核对，见 §5.6）
- [ ] 回归：`test_assets.py` 39 passed（含 t1~t31 无回归）
- [ ] 独立复验：修好 `data/scratch/verify_asset_01d_spec.py`（见 §5.5）后跑通并留档

## 5 已核结论（本会话实测，可直接引用进账本）

**5.1 幂等先于 CAS 是正确实现，派发单顺序写反了。** 实现把幂等（同 command_id 重放）放在 CAS 之前，与 `sources/fragment_service.py` F1/F4/F6/F7/F8 先例一致。实测：命令 A 改 text=A（rev1→2）→ 命令 B 改 text=B（rev2→3）→ 重发命令 A（`expected_revision` 仍=1）→ **200 且返回首次 payload（rev=2, text=A）**；若按派发单「CAS 先于幂等」则会被误判 409，破坏「同 payload 重放→200」语义（t37 也是按此设计）。→ **裁定：保留实现，修正交接单排序文字**。

**5.2 Pydantic 哨兵默认值会产生 schema 警告。** `backend/studio/scripts/api.py` 用 `object()` 哨兵做 `speaker`/`adaptation_note` 默认值，`GET /openapi.json` 仍 200，但会抛 `PydanticJsonSchemaWarning: Default value <object object ...> is not JSON serializable; excluding default from JSON schema`，并把该默认值从 schema 中剔除。影响仅为警告噪声与文档缺省值缺失。→ **建议裁定：改用 `model_fields_set` 判定「字段是否出现」**（同卡白名单内的小返修，语义更明确、无警告）；若判为不阻塞，则登记为已知限制。

**5.3 请求体类型错误走 FastAPI 原生 422 形状，不是契约的 `validation_failed` 信封。** 例：`text=123` → `{"detail":[{"type":"string_type","loc":["body","text"],...}]}`；`expected_revision="2"`/`2.0` 被宽松强转为 `2`（随后正常走 CAS）。此现象对全项目请求体形态校验一致，SCRIPT-01-a 账本已按先例接受（「缺必填体字段由请求体形态校验 422 拦截」）。→ **沿用先例，不作为本卡缺陷**，但建议记入已知限制。

**5.4 AS4 对 `name`/`type`/`character_id` 的「出现即拒绝」实现为「显式 null 视为未提供」。** 实测 `name=null` → 200（放过）；`name="改"` → 422 `forbidden`。`api.py` docstring 内部自相矛盾（先写「出现即 422」后写「null 视为未提供」）。→ **裁定：接受 null=未提供**（与「不可修改」语义一致），并修正 docstring 表述。

**5.5 `data/scratch/verify_asset_01d_spec.py` 自身有缺陷，跑不通。** 在 line 101 造「继承 media」场景时直接把 `AssetRevision.media_artifact_id` 改成不存在的 `'media-x'`，撞 `FOREIGN KEY constraint failed` 崩溃（`UPDATE studio_asset_revisions SET media_artifact_id=? ...`）。必须在 `studio_media` 里先落一行真实 media 记录，或把该场景改为「先建真实 media 行再修订」。**这张卡的独立复验目前等于没做。**

**5.6 `versions[]` 条目键集两卡一致（已核对）**：AS1 创建 DTO `versions[0]` 键 = `{created_at, media_artifact_id, review_status, revision, spec}`，与 AS4 的 `_to_dto_all_versions` 生成键集完全相同。

**5.7 环境既有事实**：`pnpm run …` 因 store 缺失 ENOENT，必须用 `web/node_modules/.bin/` 下的 `tsc`/`next` 直调；`node --test` 传目录会误报，须用通配符 `'web/tests/studio/*.test.cjs'`；`data/` 已 gitignore，复验脚本不入提交。

## 6 批次 B 收尾后的派发队列

依赖就绪（`requires` 全部 accepted）的叶卡，共 9 张，建议按域并行（互不重叠白名单）：

| 卡 | 标题 | 备注 |
| --- | --- | --- |
| `SCRIPT-02` | 采用剧本 revision 及影响 | 依赖 SCRIPT-01，本批收尾后即就绪 |
| `VIEW-01` | 新项目工作台状态容器 | 见 T6 遗留裁定 |
| `REL-02` | 资产出现引用命令 | 关系域 |
| `MEDIA-01` | 私有媒体落盘与完整性 | 解锁 ASSET-02/03、JOB 链路 |
| `CHAT-01` | 会话和消息 API | 解锁 CHAT 链路 |
| `EDIT-01` | 项目剪辑草稿读写 | 解锁 EDIT 链路 |
| `RETIRE-01` | 禁止新业务依赖旧业务的检查 | 退役域 |
| `ASSET-01-d` / `SCRIPT-01-b` | 本批两卡 | 验收后从队列移除 |

其余 109 张叶卡仍被依赖阻塞，主要瓶颈：`MEDIA-01`（解锁 ASSET-02/03）、`SHOT-01/02`（19 张）、`JOB-01~12`（21 张）、`EDIT-01~10`（20 张）、`VIEW-01~04`、`CHAT-01~04`、`UI-01~05`（11 张）、`GATE-01~06`、`RETIRE-01~04`（15 张）。

## 7 命令速查与纪律

```bash
cd /mnt/Code/agent-orchestration/workflow/Storyloom

# 单卡测试
.venv/bin/python -m pytest tests/studio/test_scripts.py -q     # SCRIPT-01-b
.venv/bin/python -m pytest tests/studio/test_assets.py -q      # ASSET-01-d
# 批次收尾
.venv/bin/python -m pytest tests/studio -q                     # 基线 448 passed
node --test 'web/tests/studio/*.test.cjs'                      # 基线 385 passed
web/node_modules/.bin/tsc --noEmit -p web/tsconfig.json
git diff --check

# 独立复验（主模型亲自做，不复用执行器自述）
.venv/bin/python data/scratch/verify_script_01a.py             # 模板
.venv/bin/python data/scratch/verify_asset_01d_spec.py         # 需先修 5.5
```

- 执行器**不得** `git add/commit`；主模型审 diff + 真跑测试 + 独立复验后才单卡提交并写账本。
- 账本 `acceptance` 结构照抄 `SCRIPT-01-a`/`ASSET-01-c`：`accepted_by`/`date`/`contract_version`(2.9)/`tested_baseline`(**该卡提交**)/`scope`/`checks`/`evidence`/`limitations`；同时更新顶层 `updated`。
- 提交信息用既有风格：`feat(studio): <卡号> <中文摘要>（要点，t 区间）` 与 `docs(studio): <卡号> 验收入账（测试基线 <sha>）`。
