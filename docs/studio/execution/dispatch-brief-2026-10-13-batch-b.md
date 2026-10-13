# 批次 B 派发交接单 · 2026-10-13

基线：分支 `studio-rebuild`，HEAD `6e58009`（批次 A 全部验收入账后）。
执行器模型：`pation/qwen38-27b-w3a4`（用户 2026-10-13 指定；同日按 profile 实际配置同步，原 `-128k` id 已不存在）。派发须在采样了新白名单的顶层会话中进行，见[执行器提示词](executor-prompt.md)。

## 派发编排

| 波次 | 卡 | 白名单 | 并行依据 |
| --- | --- | --- | --- |
| Wave 1 | SCRIPT-01-b（SC4 revise） | `backend/studio/scripts/{service,api}.py`、`tests/studio/test_scripts.py` | 与 ASSET 域零重叠 |
| Wave 1 | ASSET-01-c（物理场景分离） | `backend/studio/assets/service.py`、`tests/studio/test_assets.py` | 与 SCRIPT 域零重叠 |
| Wave 2 | ASSET-01-d（AS4 spec 修订） | `backend/studio/assets/{service,api}.py`、`tests/studio/test_assets.py` | 与 01-c 同文件，**必须串行**（01-c 验收合并后派发） |

工作树注意：`web/features/studio/shell/studio-state.ts` 为未跟踪的其他任务产出（VIEW-01 范围），
三卡派发单均已声明不修改、不删除、不暂存；其归属另行处理。

## 冻结接口（主模型裁定，附录之上补齐；冲突以附录为准并回报）

### SCRIPT-01-b = SC4 · POST `/projects/{pid}/script-objects/{oid}/revise`

- 请求：`text?`、`speaker?`、`adaptation_note?`、`expected_revision`(int 必填)、`command_id`。
  **缺席 ≠ null**：缺席=保持原值；显式 null=清空（speaker/adaptation_note）；text 显式 null 视同缺席。
  三者全缺席 → 422 `validation_failed`（field=`payload`, rule=`empty`）。
- 校验序：项目 404 → 对象 404（不存在/跨项目同形状）→ 对象 retired 422 → 字段校验
  （text 空白/speaker>80/adaptation_note>300/expected_revision 非正整数）→
  片段非 active（`source_revision_inactive`）或片段 retired 422（**不校验 confirmed**，
  附录 SC4 只列"片段非 active"）→ CAS 409 `revision_conflict` → 幂等重放/复用 422。
- 写：新 `ScriptRevision`（revision+1，缺席字段取现值，`source_fragment_revision`=保存时刻
  片段 revision）+ 对象行 `current_revision_id`/`speaker` 镜像/`updated_at` + `CommandRecord`，单事务。
- 响应 200：与 SC1 DTO 同形，revision 号为新值。**零采用写入**（测试直接查库断言）。

### ASSET-01-c · 物理场景与叙事场分离（不新建路由）

- `_validate_spec` scene 分支补声明可选键类型校验：`time_of_day`/`weather` 非 None 必须 string
  （rule=`invalid`），与 character 可选键同风格；自由扩展键仍允许。
- 分离性测试：创建 scene 资产后 `studio_scenes`/`studio_shots` 零行；叙事场行存在不干扰资产；
  资产 DTO 键集合恰为 `{object_ref,type,name,character,current_revision,versions,occurrences,bindings}`，
  无任何 `scene_id`；DTO 形状与 character 一致。
- AS1 创建主路径已由 t4/t5/t7/t8/t12 覆盖，本卡不重复。

### ASSET-01-d = AS4 · POST `/projects/{pid}/assets/{aid}/spec`（Wave 2 派发时下发全文）

- 请求：`spec, expected_revision, command_id` → 200：新 revision（=当前+1），**继承上一 revision 的
  `media_artifact_id` 与 `generation_input`**（同媒体、新规格，附录 05 AS4；本卡阶段 media 恒 NULL，
  继承语义仍落实现），`review_status` 回到 `unreviewed`，自动成为当前（改 `current_revision_id`）。
- spec 按资产 type 校验（复用 `_validate_spec`，含 01-c 补的 scene 可选键校验）。
- 409 `revision_conflict`；404 项目/资产；name/type/character_id 不可经此命令修改。
- **新 revision 不自动改旧采用/绑定**：`studio_asset_occurrences`/`studio_asset_bindings` 零写入
  （查库断言）；旧 revision 行不可变；DTO `versions[]` 含全部并存版本。

## 验收与入账

同批次 A 惯例：执行器 submitted → 主模型审 diff + 跑该卡测试 + 独立负向复验 → 单卡单提交 →
账本 acceptance（tested_baseline=当前 HEAD）→ Wave 2 → 整批收尾全量 `pytest tests/studio`。
