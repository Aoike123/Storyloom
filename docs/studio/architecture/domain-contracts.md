# 来源工作台重构实施契约

版本：2.1 · 2026-10-04（ROOT-02 附录已冻结）。方向：**按用户决定彻底重构，旧产品实现退役。** 本文为[原子计划](../execution/implementation-plan.md)的技术基线。接口/模型是本轮新建目标，不表示已实现；主模型负责冻结细节，轻量模型不得自行补兼容层。

目录和跨模块依赖以[项目结构](project-structure.md)为准；待冻结内容见[架构决策](decisions.md)。

## 1. 新模块和唯一入口

- 前端：`web/features/studio/` 新建应用、状态、组件、API客户端和样式。唯一工作台路由 `/studio/[projectId]`；公开项目详情 `/projects/[projectId]`；观看 `/watch/[releaseId]`。首页是新项目入口。
- 后端：`backend/studio/` 新建领域模型、服务、路由、任务执行、会话、媒体及交付。业务 API 统一 `/api/studio`。
- 新业务不得 import 旧 author/workbench UI，不调用 `/api/author`、`/api/director`、`/api/creative` 等旧业务 API，不包装旧阶段管线，不建立旧项目/Pxxx/Gxx迁移或路由兼容。
- 可复用经过主模型批准的基础设施：React/Next/React Flow、数据库连接与SQLAlchemy Base、账号身份验证、经提取的供应商协议客户端、纯媒体文件工具及现有主题语义。旧 `Record` 内容、`Task` 阶段和旧业务状态不作为新模型权威。
- 新代码在隔离重构分支/预览环境建设；完成门槛后一次切入新产品，删除旧产品路由、组件、业务管线与引用。代码退役和存储数据处置是两件事；本计划没有批量清库/删除历史媒体任务。

旧 `workflows.py`、`production_nodes.py`、author_flow、skill绑定、旧提示词输入输出及旧阶段测试**不是新标准**。新命令/handler/审核/调度必须由当前产品规范推导后冻结；不能因为旧模块已有字段或门槛就要求新设计迁就。

## 2. 权威模型及归属

新领域使用明确的 SQLAlchemy 表/服务，不把所有业务塞进一个显示用 JSON，不保留第二套可写树。主模型将以下聚合的字段、外键、唯一约束、revision及状态表落实到 schema 附录后，才派发 DB 卡。

| 聚合 | 权威与关键关系 |
| --- | --- |
| StudioProject | id、owner_id、name、description、visibility、active_source_revision_id；不使用旧 author stage 驱动导航 |
| StoryRevision | 不可变原文与canonical文本/hash；project_id、稳定revision身份、前一版本 |
| RangeSet / Fragment / FragmentRevision | 指向准确StoryRevision，稳定fragment_id；集合CAS；候选/确认/待复核/退役；拆合继承 |
| ScriptObject / ScriptRevision | action/dialogue等实际对象；fragment归属、来源、顺序；编辑草稿与采用revision分开 |
| Scene / Shot / ShotRevision | shot.scene_id唯一归属；采用剧本revision、画面/时长/衔接及实际输入；顺序不由坐标推断 |
| Asset / AssetRevision | 项目资产稳定ID、类型及人物/服装归属；规格、媒体、实际生成输入和审核；并存版本 |
| SourceRelation / AssetOccurrence / AssetBinding | 来源关系、原文出现、生成输入是不同关系；出现不承担归属，绑定固定资源版本/用途/作用对象 |
| ReviewDecision / ChangePreview | 确认绑定目标revision/依赖摘要；变更预览与应用校验基准；失效/保留列表 |
| StudioJob / JobAttempt / JobEvent | 唯一新任务权威；冻结目标、输入、幂等、claim/lease、尝试、真实事件/结果；不复制旧Task状态 |
| StepConversation / Message / Proposal | 项目/步骤/片段/对象版本上下文；历史、回答任务、明确采用提案 |
| EditDraft / EditInstance / ConfirmedEdit | 项目草稿、独立使用实例、不可变确认版本；采用来源和毫秒时间模型 |
| MediaArtifact / Release | 真实文件及摘要/状态；发布固定ConfirmedEdit及输出规格，公开字段白名单 |

统一 `ObjectRef={kind,id,revision}`，所有引用先验证属于当前项目、版本存在及可用性。项目资产可被多片段共用，不为每片段复制归属。版本更新不换实体ID；拆合可以产生新实体并保留继承关系。业务归属只来自上述真实外键，树视图是查询结果。

所有领域写入使用该聚合的 expected_revision 和稳定 command_id；事务内 compare-and-swap，重复command返回原结果。遇409提供准确冲突/当前版本，不静默覆盖。不能用后台频繁改变的整个项目版本代替所有领域版本。

## 3. 原文与片段

### 3.1 字符契约

- raw_content按提交内容不可变保存，保留raw_hash，不trim、不Unicode归一化。
- canonical_content仅将CRLF/CR变为LF，保留其余字符和首尾空白。高亮、选区、引用以这串文本为准。前端渲染服务端文本，不另做清洗。
- 来源返回真实source_revision_id、raw/canonical hash和 `offset_policy="lf-utf16-v1"`；不使用固定import-1。
- 范围为UTF-16 code unit半开区间 `[start,end)`；Python用明确换算helper，不直接用code point切片充当JS offset。非整数、越界、空白、空范围、劈开代理对均拒绝。
- 候选与确认共同不可重叠；`a.start < b.end && b.start < a.end`；重复、相交、包含均冲突，相接合法。AI提案执行相同服务端规则。

### 3.2 状态与写入

拖选是临时视图；明确“新建片段”保存candidate；明确“确认”才是confirmed。确认不启动生产，也不表示全文覆盖。完整制作范围提交另外检查顺序、首尾覆盖、裁减及范围外记录，增量候选允许未分配正文。

创建请求含 source_revision_id、expected_range_set_revision、command_id、range/name；服务端生成稳定fragment_id，返回真实revision。改名保留ID；改边界创建revision并预览影响；拆合生成新ID并记录predecessor_ids；旧引用不自动移给新实体。

正文更新流程为“预览影响→明确应用→新StoryRevision→来源复核”。旧范围不能套进新文本；无法确定的新映射待复核，不靠相同字串搜索假称恢复。新系统已产生的视频、剪辑、发布保留其实际生成/采用依据；运行中的任务继续使用冻结的原revision。

### 3.3 新API路由族

主模型冻结请求/响应/错误码后实施，接口都在 `/api/studio/projects/{pid}` 下：

| 路由族 | 行为 |
| --- | --- |
| `/sources`、`/sources/{revisionId}` | 导入/读取不可变来源，激活与更换走变更命令 |
| `/fragments`、`/fragments/{fid}` | 创建/读取、改名、边界；confirm/retire/split/merge显式命令 |
| `/source-changes/preview`、`/apply` | 精确变更影响与基准校验应用 |
| `/production-scopes` | 独立确认完整制作范围，不等于新建候选 |
| `/script-objects`、`/scenes`、`/shots`、`/assets` | 各自领域草稿/版本/采用/结构操作；禁止万能任意字段patch |
| `/relations`、`/bindings` | 来源关系与准确生成输入分别校验 |
| `/jobs`、`/jobs/{jid}`、`/job-events` | 冻结任务、真实进度、重试/取消等命令 |
| `/edit`、`/edit/confirm` | 项目剪辑草稿与不可变确认版本 |
| `/conversations`、`/proposals/{id}/apply` | 步骤会话与明确提案采用 |
| `/exports`、`/releases` | 固定确认版本的真实导出与发布 |

账号继续使用批准的身份验证入口；新业务每次HTTP读写严格验证属主。公开查询仅 `/api/studio/public/*`，不返回原文、过程资产、私有对话。读请求不得顺便创建片段/任务或迁移记录。

## 4. 工作台与来源关系

- 来源区贯穿项目，原文/动态故事片段两视图共用稳定ID。原文保留原生段落，不插片段卡片、标题、状态。默认浏览/选择，不把普通点击当编辑。
- 第一版沿用五种职责ID：cut/script/board/assets/film，名称仍可由主模型随设计调整。后端任务状态不能决定用户标签。
- 切分/剧本/分镜为无限画布；资产也是无限画布，项目共用。节点自由拖动、空白平移、缩放，坐标不改父级/顺序/引用。默认能看项目多个场/镜头，不恢复旧常驻目录/场次筛选器。
- React Flow受控节点从实际模型派生，坐标另存；增删/字段变化立即同步。统一ObjectRef选择控制节点、属性和来源定位，只有一个正在查看的对象。
- 按 `(account,project,fragment,workspace)` 保存制作上下文；资产/成片保存项目上下文。视图缓存带schema/来源版本，不保存业务确认或token。
- 剧本一整块区域对应多个动作/对白对象：一个真实边缘起点、一条共同主干再分支。取实际可见painted client rect，不能取多行包围盒的虚空右中点或侧栏外沿。
- 锚点算法第一版：过滤阅读视口内可见涂色行，取中间可见行的右边缘与垂直中点；目标通过登记ref定位。整区离屏隐藏线并提供定位。滚动/换行/栏宽/拖动/缩放更新几何，显示当前关系，不修改数据。
- 资产多处出现可指向同asset/revision，出现标记是次级出处，不创建新故事片段。镜头生成绑定固定资源用途/作用对象/版本，不能以显示连线代替。
- 属性显示选中真实对象的身份、归属、来源、版本、状态和可编辑字段；成片选中的是实例，资源选择与实例选择分开。

## 5. 新生产调度

不复刻旧九阶段author_flow。生产以明确对象及其revision的命令/任务推进：采用剧本→生成/采用分镜→准备/审核资产→冻结镜头输入→生成/审核视频。每类任务处理器独立，所需输入由主模型固定。

任务冻结 `project_id,fragment_ids,target_ref,input_revisions,input_digest,command_id,kind`；服务端解析明确范围，不在运行时把“当前片段”补进目标。长作品按显式目标集合与可配置执行窗口安排，窗口预算不能成为整个项目片段数量限制。

任务状态为queued/running/needs_review/completed/failed/cancelled/superseded；attempt、供应商请求ID和租约独立保存。提交成功不是供应商完成，任务完成不是作者审核/采用。重试、继续、重做语义由主模型给定，迟到结果保留在原目标，不自动采用或抢焦点。

ROOT-04须按每一种新操作登记目标/触发、前置依赖、输入输出、状态、用户确认点、影响与失败恢复，分别定义同步命令/异步任务/会话提案；不能以旧workflow节点名或旧测试断言替代此表。

主模型负责供应商adapter：可以提取已验证的协议/配置/媒体工具，但必须解除对旧Record/Task/author_flow/reader_branch的业务依赖。新StudioJob是唯一状态权威。所有验证使用假供应商和本地媒体，真实生成由产品中的明确用户动作触发。

## 6. 成片、媒体与发布

```text
EditInstance = {id,track_id,timeline_start_ms,source_in_ms,source_out_ms,
  fragment_id,source_revision_id,shot_id,shot_revision,clip_id,clip_revision,
  media_digest,speed,audio_settings,effects}
ConfirmedEdit = {id,edit_id,edit_revision,manifest_digest,output_spec,instances_snapshot}
```

时间存整数毫秒。项目有一份当前编辑草稿，instance每次取用独立ID；源clip/revision固定。裁切/重排/删除不改源镜头与视频。保存用edit自身版本和幂等命令，返回/刷新可恢复。

成片不用自由画布；编辑组件占满主区，左侧来源列表就是实际视频资源区。来源点击只浏览/预览，不跳出film、不替换时间轴；明确插入才写实例。属性按需覆盖/展开；底部有同等对话入口。

主模型先做内核PoC，冻结一个预览/导出adapter及支持范围、依赖许可证、格式、错误恢复。预览与导出消费同一规范化manifest；不得用固定96秒、定时器或图标冒充媒体。

发布和导出绑定同一个ConfirmedEdit与output_spec，历史发布不可变；草稿修改不改变已发布内容。导出失败可以同版重试，不重新生成镜头。新观看页面消费新Release，不适配旧分片发布。

私有媒体存储与公开发布媒体访问分别设计：不能把私有原文/过程媒体挂到无鉴权的公开目录。公开观看只访问该Release明确定义的产物，所有文件经过实际落盘/完整性检查。

## 7. 步骤对话

上下文 `{project_id,workspace,fragment_id?,selection_ref?,source_revision_id,range?}` 在发送时冻结，消息/回答/提案归原会话。切步骤/片段恢复相应历史与未发送输入；后台回答不切标签。

五步骤均有固定底部自然语言入口，不随画布缩放，不要求agent/model名称。真实问答可读取冻结范围的状态/出处；AI只提出结构化proposal。用户明确采用后，走对应领域命令及版本/影响校验；回答本身不确认、不生成、不采用、不发布。

## 8. 共用边界fixture与冻结门槛

| 输入/操作 | 期望 |
| --- | --- |
| raw=`甲😀乙\r\n\r\n“走吧。”\n末行` | canonical=`甲😀乙\n\n“走吧。”\n末行`，UTF-16长14 |
| 空集合建[0,4)，再建[4,11) | 都成功，相接合法、两个稳定ID |
| 再建[0,4)、[3,7)、[0,11)、[1,3) | 重复/相交/包含/被包含均冲突，给出现有fragment ID |
| [1,2)、[2,3) | 代理对被劈开，拒绝 |
| [4,6)、[2,2)、[-1,3)、[0,15) | 空白/空/越界拒绝 |
| 同command重试；两命令以同版本争同范围 | 同结果；最多一成功，另一409且无半次保存 |
| A慢响应在切B后返回 | 不混入B，结果和任务仍归A |
| 同clip插两次，再删一次 | 独立实例，剩下实例与源clip不变 |

ROOT-02 必须落实表/字段/API/CAS/状态/影响附录；ROOT-03落实媒体内核；ROOT-04落实新生成/agent adapter和提案执行。未冻结的后继卡标blocked_by_design，不能让轻量模型补TODO/兼容层交付。

## 9. 冻结附录（ROOT-02 产物，2026-10-04）

具体表、字段、命令请求/响应/错误码与 impact 以附录为准；本文件既有小节与附录冲突时以附录为准（附录是更精确的冻结）。任务卡派发时把对应附录页随卡发送。

| 附录 | 覆盖 |
| --- | --- |
| [00 · ObjectRef 与共享协议](appendices/00-object-ref-and-protocol.md) | ObjectRef/kind 登记、ULID/CAS/command_id 幂等、错误协议、预览-应用协议、查询 DTO、范围校验规则 |
| [01 · 项目与不可变来源](appendices/01-projects-sources.md) | studio_projects、studio_source_revisions；P1–S6 与公开端点白名单 |
| [02 · 范围集合与片段](appendices/02-fragments.md) | range_set 集合 CAS、fragment/fragment_revision、production_scope；F1–F12 与拆合/边界/退役 impact |
| [03 · 剧本对象与采用](appendices/03-scripts.md) | script_object/revision/adoption 采用链；SC1–SC7 |
| [04 · 场与镜头](appendices/04-scenes-shots.md) | scene（layout CAS）、shot/shot_revision；SB1–SB9 与显式重排、生成入口 |
| [05 · 资产与并存版本](appendices/05-assets.md) | asset/asset_revision（spec 必填键、review_status、当前指针）；AS1–AS10 |
| [06 · 关系与绑定](appendices/06-relations.md) | source_relation、asset_occurrence、asset_binding（用途/作用对象/双服装校验）；RL/OC/BD 命令 |
| [07 · 变更预览与审核](appendices/07-reviews.md) | change_preview、review_decision；RV1–RV5 与 kind 冻结清单 |

Job/会话/剪辑/媒体/发布五族的表与命令由 ROOT-04/ROOT-03 在本目录补附录（kind 已在 00 登记）；其 DB 卡（DB-08/09/10）与 CHAT/EDIT/MEDIA 卡在那之前保持未就绪。
