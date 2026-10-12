# D03 · 精确文字范围契约

版本：1.1 · 2026-10-12 · 状态：契约已定，前后端已落地（后端 `source_ranges.py`＋`authors.py`，前端 `source-model.ts`＋`SourceArea.tsx`，25 项范围测试通过）

本文件定稿 D03 的精确文字范围契约：字符计数单位、换行规范、正文版本、半开区间、冲突校验、旧段落引用迁移与正文变更影响规则。设计依据见[故事总控](canvas-design-direction.md#7-故事总控连续原文与荧光片段)、[精确范围契约方向](canvas-design-direction.md#11-精确范围契约与后续工程)及[设计任务骨架 D03](design-task-skeleton.md#d03--原文切分与故事总控)。

## 1. 字符计数单位

**正文字符偏移一律以 Unicode 码点（code point）计数。**

- Python：`len(str)` 与切片 `text[start:end]` 原生按码点计数，直接可用。
- JavaScript：`String.prototype.length` 与 `slice` 按 UTF-16 码元计数，与码点不同（增补平面字符如 emoji 占 2 个码元）。前端必须经 `source-model.ts` 的码点换算函数（`utf16ToCodePointOffset` / `sliceCodePoints`）换算，**不得把 JS 字符串索引直接当作契约偏移**。
- 持久化的 `start` / `end` 是码点偏移；前后端在同一规则下换算，不依赖 DOM 节点序号、显示段号或画布坐标（遵[设计方向 §11](canvas-design-direction.md#11-精确范围契约与后续工程)）。

## 2. 换行规范

- 存储正文即导入时收到的原始字符串，**不做任何规范化**（不折叠空白、不统一换行）。
- 换行符按其在存储字符串中的码点原样计数：`\n`（U+000A）与 `\r`（U+000D）各占 1 码点。
- 范围定义在存储字符串上；同一字符串在前后端用同一规则计数，跨端换算只发生在码点↔UTF-16 之间。
- 导入入口（`POST /projects/{pid}/story`）继续做非空与 2MB 上限校验；范围校验不重复内容校验。

## 3. 正文版本（revision）

- 正文版本 = 存储正文的 SHA-256 十六进制摘要（64 位），即 `story_source.data.content_hash`（导入时已生成）。
- 每个片段记录创建/更新时的 `revision`（当时正文的摘要）。
- 任何范围写入请求必须携带 `expected_revision`（客户端所依据的正文版本）；服务端与当前 `content_hash` 比对，**不匹配返回 409，不悄悄挪动范围**（并发正文或边界变化返回明确冲突）。
- 正文重新导入（更换或改写）产生新版本；旧片段保留其 `revision` 与范围，标记 `stale`（待复核），不静默移动、不按相同文字搜索后宣称已恢复（遵[设计方向 §7.4](canvas-design-direction.md#74-正文和范围修改)）。

## 4. 半开区间与合法性

- 范围用半开区间 `[start, end)` 表达，等价于 `start <= i < end`。
- 合法条件（服务端与前端同一规则）：
  1. `start`、`end` 为非负整数；
  2. `0 <= start < end <= len(content)`（码点）；
  3. **非空**：`content[start:end]` 至少含一个非空白码点（空白选区拒绝）。
- 相邻区间可共享边界（`a.end == b.start` 允许）；重叠判定为 `a.start < b.end && b.start < a.end`。

## 5. 冲突校验

- 已保存候选（`candidate`）与已确认片段（`confirmed`）**都参与**冲突检查（遵[设计方向 §7.2](canvas-design-direction.md#72-一个连续荧光区域对应一个故事片段)）。
- 重复、部分相交、完全包含均阻止；冲突响应指出冲突片段的稳定 ID 与名称，不静默裁掉重叠文字、不覆盖边界、不新增重复片段。
- AI 提出的候选执行同一规则。
- 拆合／多片段变更作为一组整体校验：批次内每个范围合法、批次内互不重叠、与已有片段互不重叠，全部通过才提交。

## 6. 稳定片段 ID 与状态

- 稳定片段 ID 形如 `F01`、`F02`…，由服务端在创建时分配，项目内唯一，**永不复用**（删除后编号不回收）。
- 状态：`candidate`（待确认）→ `confirmed`（已确认）。确认是独立操作；候选不因建立、浏览或 AI 返回而自动确认。
- 列表与荧光区域共用同一稳定 ID；点击已有区域／列表项跳转，不重复创建。

## 7. 候选检查与完整方案检查分开

- **候选检查**（增量）：单个范围合法、非空、与已有片段不重叠。允许存在未分配正文——不因全文尚未切完就阻止第一次划选。
- **完整方案检查**（提交完整切分时）：针对明确的制作范围检查来源顺序、首尾覆盖，并明确呈现漏覆盖、裁减或范围外内容。完整方案校验与候选校验分开（遵[设计方向 §7.2](canvas-design-direction.md#72-一个连续荧光区域对应一个故事片段)）。
- 旧流程的数量预算（`MAX_SEGMENTS` 等）继续用于旧计算窗口，不扩大为整部故事上限。

## 8. 旧段落引用迁移

- 旧 `source_refs`（P 编号）来自 `director.source_passages`：`P{n}` 覆盖 `content[(n-1)*240 : n*240]`（码点窗口，每窗 240 码点）。
- 迁移映射：一段的 `source_refs`（升序、不重复、连续）合并为单一连续范围 `[ (first-1)*240, last*240 )`；窗口边界即字符边界，不引入额外偏移。
- 迁移产生的片段 `origin = "migrated"`，`revision` 取当前正文摘要；迁移只建立范围记录，不改变旧流程的段落级校验（`segments.py` 的 `plan_issues` 继续用于旧计算窗口）。
- 迁移保留历史文本、片段及产物依据：旧 `plan_dump` 的 `source_text` 与片段文本依据继续可读。

## 9. 正文变更影响规则

- 保存正文、确认片段、生成、采用和发布是不同动作。
- 正文新版本使旧片段 `stale`：相关剧本和分镜标记待更新；共用资产保留真实归属；已有视频、已确认剪辑与历史发布保留生成／采用时的正文与输入版本，不静默替换。
- 失效映射标记待复核，不能仅挪动高亮或按相同文字搜索后宣称已恢复；人工复核通过后才更新片段的 `revision` 与范围。

## 10. 接口契约

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/author/projects/{pid}/source/fragments` | GET | 列出片段（含 `stale` 标记） |
| `/api/author/projects/{pid}/source/fragments` | POST | 建立候选片段（范围 + `expected_revision`） |
| `/api/author/projects/{pid}/source/fragments/{fid}/confirm` | POST | 确认候选片段 |
| `/api/author/projects/{pid}/source/fragments/{fid}` | PUT | 调整边界／名称（整组冲突校验） |
| `/api/author/projects/{pid}/source/fragments/{fid}` | DELETE | 删除片段 |
| `/api/author/projects/{pid}/source/fragments/plan` | POST | 提交完整切分方案（顺序 + 首尾覆盖 + 裁减／范围外） |
| `/api/author/projects/{pid}/source/fragments/migrate` | POST | 从旧段落引用迁移 |

前端即时冲突反馈与服务端最终校验一致；`GET /projects/{pid}` 的 workspace 响应附带 `source_revision`（当前正文摘要）与 `fragments`（真实片段列表），工作台据此构建来源模型，不再用隔离示例冒充已保存记录。

## 11. 验证范围

- 本契约只验证范围规则：连续非空、边界合法、重叠／相接、版本匹配、迁移映射、完整方案覆盖。
- 不执行全量后端测试，不调用模型或媒体供应商，不等待生产任务。
- 前端 `checkRange` 与服务端 `source_ranges` 共用同一规则；像素级几何（来源连线、窄屏断点）待真机走查。
