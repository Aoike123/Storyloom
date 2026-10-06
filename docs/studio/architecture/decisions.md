# 架构决策与冻结清单

版本：2.0 · 2026-10-04。此文登记决定和待定点，不维护第二套产品规范或任务进度。

## 已定边界

| 决定 | 依据/落实位置 |
| --- | --- |
| 全面重构，用户明确允许删除旧版代码；不做旧格式迁移/兼容 | 用户最新决定；[项目结构](project-structure.md) |
| 连续原文、整块荧光片段、单主干剧本连线、真实对象属性、每步对话 | [R02–R06/R10](../product/product-spec.md) |
| 成片采用全尺寸剪辑，左侧来源列表就是资源区 | [R09](../product/product-spec.md#r09-成片资源与编辑) |
| 按领域拆前后端，新工作台与API分别使用studio命名空间 | [项目结构](project-structure.md) |
| 旧后端workflow/节点/提示词契约/专用测试不再作为标准，新流程重新定案 | 用户最新决定；[领域契约](domain-contracts.md) |
| 原文不可变版本、UTF-16范围、新领域表与StudioJob、固定采用依据 | [领域契约](domain-contracts.md) |
| 当前规范/架构/任务/证据/历史分工；状态只在账本中维护 | [文档入口](../README.md) |

## 主模型实施前必须冻结

| 门槛 | 必需输出 | 未完成时禁止派发 |
| --- | --- | --- |
| ROOT-01 | 准确checkout/分支/HEAD、依赖运行时、隔离数据和预览端口 | 应用代码修改与切换端口 |
| ROOT-02 | 按领域的字段/外键/唯一性、命令请求响应、错误码、CAS、状态表、跨域事务与基础设施白名单 | 对应DB/API/界面命令 |
| ROOT-03 | 实测内核PoC、manifest、支持操作、预览/导出一致性与媒体格式 | 预览/速度/轨道/导出等媒体操作 |
| ROOT-04 | 每种任务adapter输入输出、租约/重试/结果核实、上下文读取、proposal白名单 | 生成/对话/自动处理器 |

ROOT-02将结果写入架构目录的按领域附录，并从领域契约链接；ROOT-03/04也在此目录登记实测方案。文件和接口未定义时标未就绪，轻量模型不得猜选型、补兼容分支或用占位结果通过验收。

RETIRE-01还须由主模型冻结基于实际实施HEAD的逐文件退役清单，区分删除、解耦、提取及其引用方/复验范围。只读依赖盘点不等于删除白名单。

变更应写明日期、决定、原因、影响任务、复验范围。产品行为变化先更新产品规范；技术决定改变再更新相应附录和账本。不得在执行器返修消息中悄悄引入另一套契约。

## 变更记录

| 日期 | 决定 | 原因 | 影响任务 | 复验范围 |
| --- | --- | --- | --- | --- |
| 2026-10-04 | ROOT-01 完成：实施基线固定为 studio-rebuild@bcb3eb4（主目录），预览端口 3011/3021，新数据根 data/studio | 隔离重构、不触碰旧栈端口（3000/8000/3001）与他处运行实例 | 全部后续卡（白名单中的实施目录/端口以此为准） | 每次派发核对账本 implementation 块 |
| 2026-10-04 | ROOT-02 完成：appendices/ 八个附录（00 共享协议 + 01–07 分域）冻结 13 聚合中 8 组、全部 DB-01～07 与 SOURCE/SCRIPT/SHOT/ASSET/REL 命令；契约版本 2.0→2.1（仅增附录链接，既有小节语义不变） | 按 R 规则与契约把表/字段/命令/CAS/状态/impact 落到可照单实施的程度 | DB-01～07 及 SOURCE/SCRIPT/SHOT/ASSET/REL/VIEW 卡获得就绪资格（以账本为准）；DB-08/09/10、CHAT/EDIT/MEDIA 仍依赖 ROOT-03/04 | 派发时以附录为唯一接口依据核对任务卡；发现冲突改任务卡，不改附录语义 |
| 2026-10-04 | 片段“待复核”（pending_review）落实为**读时派生状态**：片段绑定版本≠当前正文版本时 DTO state 显示 pending_review，不落库；重新激活旧版本自动消失 | 避免激活切换产生状态回写/丢失；R11“需复核”语义由派生标记完整满足 | DB-02、SOURCE 各卡 | 片段列表/详情验收核对派生 state 与 revision_is_active |
| 2026-10-04 | 片段退役一律走 preview/apply（加入 00 §5 必须预览清单）；被采用引用的剧本对象不可直接删除（SC5 拒绝并提示重新采用） | R11 失效标记≠物理删除；保留历史依据可追溯 | SOURCE-05-b/14-c、SCRIPT 卡 | 退役/删除的 impact 与拒绝路径验收 |
| 2026-10-04 | 项目表不存封面字段；公开列表/详情封面取自最新公开 Release 的 poster | 避免 project→asset/media 反向外键环；R12 封面需求在发布层满足 | DB-01、UI 卡、ROOT-03/04（Release 含 poster 字段） | 公开首页/详情/观看页验收封面来源 |
| 2026-10-04 | 正文更新只支持整篇重导入 + 激活预览切换，不提供局部文本 diff 编辑命令 | R03“修改当前正文形成新版本、不设第二常驻稿件栏”；最小表面、无第二可写文本 | SOURCE-09/10（按整篇语义实现 preview/apply） | 来源变更流程验收 |
| 2026-10-04 | ROOT-03 完成：附录 08 冻结剪辑内核 —— ffmpeg 7.0.2-static 子进程导出（GPL 进程隔离，venv 已含、不新装不升级）、浏览器原生预览消费同一 EditManifest v1、MP4/输入白名单、私有/公开存储分离、取消=杀进程组、失败=**同 ConfirmedEdit 同版重试**；PoC 6/6 实测通过（切点/时长/音高精确一致），fixtures 已提交 | R09 真实文件、同版重试、不展示假效果 | EDIT/MEDIA/GATE-04/05 卡就绪资格（以账本为准） | 派发时以 PoC 脚本为提取基线核对 infrastructure/media；浏览器侧留 GATE 验收 |
| 2026-10-04 | ROOT-04 完成：附录 09/10/11 冻结 StudioJob 族（5 个 job kind 注册表）、会话/提案（白名单 21 类、作用域、**响应不直写业务**）、媒体/确认剪辑/发布（同名版本共存、公开白名单、poster=项目封面）；契约 2.1→2.2，13 聚合全部冻结 | 新流程不得绑定旧节点名/prompt 负载/旧测试断言；StudioJob 为唯一任务状态权威 | DB-08/09/10、JOB/CHAT/EDIT 卡就绪资格（以账本为准） | 派发时核对适配器抽取白名单（禁 import 旧 providers/workflows/worker 业务路径）；不确定收费提交只轮询/人工确认重试 |
| 2026-10-04 | 供应商适配器：只抽取旧 `llm_stream/image_provider/providers` 的**纯协议**（SSE/stall/usage、siliconflow 校验、minimax/ark 提交轮询、https 守卫），task_id→job/attempt、DATA/media→新存储、image_errors/provider_usage→attempt 字段与事件；`workflows.py`/`worker.py`/diagnostics 整体不抽取 | 隔离新旧、无 stage/node-skill 残留 | JOB-01..08、CHAT 卡（infrastructure/providers 交付） | 抽取 diff 审查 + import 边界测试 |
| 2026-10-04 | 附录 00 增 §8 前端 HTTP 客户端传输（base=3011 可 env 覆盖、Bearer+localStorage、command_id 每动作一次且重试复用、30s 超时→network 错误、失败不转成功空数组）；契约 2.2→2.3 | BASE-02 需要可照单实现的传输层冻结 | BASE-02（及后续所有 web 域卡） | 派发 BASE-02 时按 §8 核对 client 行为 |
| 2026-10-04 | 附录 11：`UNIQUE(project_id,name)` 修正为**条件唯一索引**（仅 status IN draft/published 存活行受约束，SQLite partial index） | 原 plain UQ 与 §3/R12“同名再发布=新 published 行 + 旧行退役保留可看”数学矛盾（两行同名必然并存）；条件唯一保留创建时 duplicate_scope 语义并放行退役版本链 | DB-10、EDIT-08/09/10、GATE-05/06；契约 2.3→2.4 | 发布/观看验收核对：同名再发布后旧行 retired 仍在库、public 目录保留、/watch/{旧id} 可播；新 published 行 predecessor 指向旧行 |
| 2026-10-04 | 附录 00 §7 增 **blank 空白集冻结** = JS `\s` 码点集（U+0009~0D/U+0020/U+00A0/U+1680/U+2000-0A/U+2028/29/U+202F/U+205F/U+3000/U+FEFF）；Python 不用 isspace()，两栈显式同集 | 实测两栈对 U+0085/U+001C-1F/U+FEFF 等码点 blank 判定双向分歧（Python isspace() ≠ JS \s），客户端预校验与服务端权威会在任意文本上互相矛盾 | SOURCE-02、SOURCE-12 及所有范围校验调用方；契约 2.4→2.5 | 码点级跨栈断言（test_ranges.py 与 ranges.test.cjs 同名 10 条）+ fixture 9 案例仍全过 |
| 2026-10-04 | 附录 00 §4 增错误码 **`duplicate`（409，details `{"scope_ref"}`）**：属主级身份唯一冲突（P1 项目名 (owner,name) UQ）；与既有 `duplicate_scope`（422，覆盖范围重复）语义不互换 | 附录 01 P1 冻结“409 属主内重名”，但冻结 11 码表无 409 级重复身份码（duplicate_scope 为 422 且语义限制作范围）——两冻结文本冲突，按 00 §4 自规则“新增需升版本”处置 | SOURCE-01-a 及后续属主级 UQ 卡（发布创建仍用 422 duplicate_scope，附录 11 §3 不变）；契约 2.5→2.6 | 重名/跨属主/幂等重放三路径验收 |
| 2026-10-04 | 附录 02 §2 F4/F5 失败码 **422 revision_conflict → 409 revision_conflict**（笔误修正；F5“同上”随 F4 修正） | 附录 00 §4 冻结 revision_conflict=409，且同表 F6 行正确写 409——F4/F5 单元格与协议附录互斥，按协议附录为准修正；契约 2.6→2.7 | SOURCE-05-a（改名）/05-b 所在 F4/F5/F7 卡；仅改码表映射，不改命令语义 | rename/summary 的 CAS 失败路径按 409 验收 |
| 2026-10-04 | 附录 02 §2 F7 preview 失败码 **409 precondition_failed → 422 precondition_failed**（笔误修正；apply 行补全 409 标注） | 附录 00 §4 冻结 precondition_failed=422；同表 F9/F10 行未标 409（默认按 422）——F7 单元格与协议附录互斥，沿用 v2.7 先例以协议附录为准；契约 2.7→2.8 | SOURCE-05-b（退役 preview/apply）及后续 F8/F9/F10 卡；仅改码表映射 | 退役 preview 已退役路径按 422 验收 |
| 2026-10-06 | 附录 01 S4 + 附录 07 RV1 preview 成功形状 **"201 ChangePreview/预览 DTO"→ 200 `{preview_id, kind, baseline, impact}`**（笔误修正；随行 S4 baseline 键名精确为 `target_canonical_hash`、"409 preview 重放"修正为"重放 200 原结果"、impact 引用 §3→§2 激活语义） | 00 §5.1 冻结 preview 返回 `{preview_id, kind, baseline, impact}`，且 F7–F10 四个已验收实现均 200 恰 4 字段——S4 行与 RV1 行同协议附录及已验收行为互斥，沿用 v2.7/v2.8 先例以协议附录为准；契约 2.8→2.9 | SOURCE-09/10（source_activate preview/apply）及后续各领域 preview 卡；仅改成功形状，不改命令语义 | 激活 preview 按 200 恰 4 字段验收 |
