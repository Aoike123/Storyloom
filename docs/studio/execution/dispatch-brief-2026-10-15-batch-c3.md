# 批次 C3 派发交接单 · 2026-10-15（波次 C3）

基线：分支 `studio-rebuild`，HEAD `c84026f`（REL-02 验收入账后，工作树干净）。

## 与批次 C 的差异：执行器模型改选

批次 C（2026-10-13）三卡 SCRIPT-02 / MEDIA-01 / VIEW-01 **全部零产出失败**，根因是原执行器模型 `pation/qwen38-27b-w3a4` 的 contextWindow 仅 65536（账本 `observed_baselines.executor_context_limit_2026_10_13`）。2026-10-15 复测该端点 `max_model_len/contextWindow/context_length` **仍全为 65536**，故本波次**改选路线**：

- `provider=kimi-coding`、`model=kimi-for-coding-highspeed`（contextWindow **262144**）
- 不再受批次 C 的「单卡阅读+产出 ≤ 50KB」预算约束；派发单可给出完整冻结规格
- 记录同步：[执行器提示词](executor-prompt.md) 已更新模型段；回退 pation 时须重新适用其预算节

## 波次与并行依据

| 卡 | 白名单 | 并行依据 |
| --- | --- | --- |
| MEDIA-01（私有媒体落盘与完整性） | `backend/infrastructure/media/probe.py`、`backend/studio/media/store.py`、`tests/studio/test_media.py`（+ `backend/infrastructure/**/__init__.py`） | 与 CHAT/EDIT 零重叠；**无 HTTP 路由**，不碰 `router.py` |
| CHAT-01（会话和消息 API） | `backend/studio/conversations/{service,api}.py`、`tests/studio/test_conversations.py` | 零重叠 |
| EDIT-01（项目剪辑草稿读写） | `backend/studio/edits/{service,api}.py`、`tests/studio/test_edits.py` | 零重叠 |
| SCRIPT-02（SC6 采用剧本 revision 及影响） | `backend/studio/scripts/{service,api}.py`、`backend/studio/reviews/impact.py`、`tests/studio/test_scripts.py` | 零重叠 |

四卡文件集两两不重叠（MEDIA-01 建 `backend/studio/media/store.py` 新文件，不触及 CHAT/EDIT 需要的 `models.py`，故与二者无交集）。

SCRIPT-02 追加进本波次的依据：其独有的 `backend/studio/reviews/impact.py` 不与前三卡相交，且 `scripts.api` 早已在「存在即挂载」清单内。**代价与约束**：并发 pytest 会争用同一进程单例测试库（drop/create 同名表），故本轮**验收测试一律串行回收后执行**，不并行跑 pytest。

**只读**：`backend/studio/router.py`（「存在即挂载」清单已含 `conversations.api`/`edits.api`/`scripts.api`，三卡均无需接线；`media.api` 不在 MEDIA-01 范围）、`tests/studio/conftest.py`、各域 `models.py`、`backend/studio/contracts/**`。

**本波次不派卡说明**：
- VIEW-01 已由**主模型亲自实现并验收**（提交 `730b23a`，账本入账 `282e4e4`）：采纳 `e06939f` 冻结容器草案，接线 `StudioApp.tsx`（`useReducer` + `projectId` prop → `setProject`），补写 `state.test.cjs` 28 例。独立复验：`node --test 'web/tests/studio/*.test.cjs'` → **413 passed / 0 fail**（基线 385）；`tsc --noEmit -p web/tsconfig.json` → exit 0。

## 通用冻结约定（三卡共同）

- 鉴权 `core.auth.require_user`（严格 Bearer/`sl_auth`，无匿名回退）；
- 错误一律走 `StudioAPIError`（`backend/studio/contracts/errors.py`）冻结形状 `{"error":{"code","message","details"}}`（附录 00 §4）；禁止 `raise HTTPException`；
- 写命令幂等：`(owner_id, command_id)` 命中 `CommandRecord` 且首次 payload 一致 → 返回首次 `result_payload`（**HTTP 200**，覆盖 201）；异内容 → 422 `validation_failed` field=`command_id` rule=`reused`，零写零 commit。放置位置照各卡既定先例（幂等先于 CAS/状态检查）；
- CAS 失败 → 409 `revision_conflict` details `{"object":{"kind","id"},"expected","actual"}`，真实值；
- 既有实现模式直接读：`backend/studio/projects/{api,service}.py`（路由薄层 + service 业务 + `_replay` 标记 + `JSONResponse(200)` 覆盖 201）；
- 测试沿用 `tests/studio/conftest.py` 的 `client` 夹具；`db` 夹具照抄 `tests/studio/test_projects.py` 顶部（`PRAGMA foreign_keys=OFF` + drop_all/create_all）；
- 不得 import 任何旧后端模块（R01/R11）。

## 验收与入账（不变）

执行器只提交，**不得 `git add/commit`**；主模型审 diff → 真跑本卡测试 → 独立复验 → 单卡提交 → 账本 `acceptance`（`tested_baseline`=该卡提交）→ 整批收尾全量 `pytest tests/studio`。

**基线数字（本波次开工时实测）**：`pytest tests/studio/` **460 passed**；`node --test 'web/tests/studio/*.test.cjs'` **385 pass**；`tsc --noEmit -p web/tsconfig.json` exit 0；`node --test 'web/tests/*.test.cjs'` 64 pass / 11 fail（11 项全为 pre-rebuild 遗留，非本波次回归）。

## 剩余依赖波次（按账本 `requires` 推演，至全量 accepted）

| 波次 | 卡 |
| --- | --- |
| C3（本轮） | MEDIA-01、CHAT-01、EDIT-01、SCRIPT-02（VIEW-01 已由主模型验收 `730b23a`） |
| C4 | RETIRE-01（待主模型按实施 HEAD 出逐文件退役清单） |
| 2 | ASSET-02-a、JOB-01、MEDIA-02-a、SHOT-01-a、VIEW-02 |
| 3 | ASSET-02-b、JOB-02、MEDIA-02-b、SHOT-01-b、VIEW-03 |
| 4 | ASSET-02、JOB-03、MEDIA-02、SCRIPT-03-a、SHOT-01-c、VIEW-04 |
| 5 | ASSET-03-a、CHAT-02、JOB-04、JOB-05、JOB-06、JOB-08、JOB-11、SCRIPT-03-b、SHOT-01-d |
| 6 | ASSET-03-b、JOB-07、SCRIPT-03、SHOT-01-e |
| 7 | ASSET-03-c、REL-01、SHOT-01-f |
| 8 | ASSET-03-d、SHOT-01-g、UI-01 |
| 9 | ASSET-03、SHOT-01-h |
| 10 | SHOT-01-i |
| 11 | SHOT-01-j |
| 12 | SHOT-01（父卡收口） |

> 波次为依赖推演的**最早可行**顺序，非承诺排期；主模型按并行度与文件冲突逐轮重排。GATE-01～06 在各自依赖齐备后触发，不参与上述叶卡波次。
