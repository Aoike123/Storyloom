# 来源工作台实现核对 · 2026-10-04

> 历史基线观察：此报告针对d43b51f，不表示整理时较新HEAD已经重新通过浏览器核对。当前规则从[文档入口](../README.md)读取。

本次对应用户反馈：3001 首页和各工作区不符合预期，且新旧工作台混用。核对主目录提交 `d43b51f`，以已确认的[画布设计方向](../archive/pre-rebuild/workbench/canvas-design-direction.md)和[来源工作台交互稿](../prototypes/source-studio.html)为基准。

## 结论

入口迁移先于制作能力迁移：新首页将用户送进只接通原文导入的新工作台，生成、审核、重试和发布仍由旧工作台承载。新页面又把示例来源模型和本地交互状态用于真实项目，因此同一项目存在两套片段身份、操作方式和进度表达。前端虽被记录为完成，关键交互未经浏览器验收，并且已复现多个阻断使用的问题。

这轮设计在文档里有明确记录；偏差发生在任务实施、真实数据适配和验收上。服务端契约尚待细化不能解释已经出现的画布不更新、连线不显示、成片切换丢失等前端问题。

## 运行来源与入口

- 实测 `http://127.0.0.1:3001/` 返回新项目首页，3001 的 Next 进程目录为 `/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web`。本次所见页面来自主目录新代码。
- `/author` 和 `/author/workbench` 都仍可访问。旧页面实际显示“返回故事市场”“微小说”等旧文案。
- [ProjectHome.tsx](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/ProjectHome.tsx:126)的新建、继续操作进入新工作台；[旧作者页](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/page.tsx:37)保留生产进度订阅及制作操作；[新工作台入口](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/page.tsx:10)则明确选择与旧生产流程并存。
- 因此“新旧混用”不是观察错觉，也没有证据表明是浏览器随机切换版本：两套路由与职责确实同时存在。新页面没有提供完成制作的统一路径；这里只确认能力分布，没有声称它会自动跳转到旧页面。
- 当前 Codex 工作树停在 `eccfa99`，主目录已到 `d43b51f`，另有设计文档未提交。这是后续实施需处理的版本风险，但不是 3001 当前页面偏差的运行原因。

## 主要缺口与证据

| 优先级 | 问题与实际影响 | 实现证据 |
| --- | --- | --- |
| P1 | **真实项目加载示例片段。** 刚导入时片段为空，重新初始化却默认按两段一组生成最多三个 Fxx 片段，带“用户切分”“AI 建议·用户确认”“已确认”等预置状态。后端已有 Gxx 切分不参与来源列表。示例标记并未阻止它们占用原文选区或参与跳转。 | [page.tsx:61](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/page.tsx:61)、[source-model.ts:67](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/source-model.ts:67) |
| P1 | **来源状态无法稳定恢复。** 用户新建候选只存于 reducer，刷新不保留，没有候选确认操作；更换正文只更换数据和起始标签，旧候选、范围及上下文仍留下。正文版本用 completeness 派生的 import-1/import-0 代替真实身份，不同正文无法据此区分。 | [workspace-state.ts:110](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/workspace-state.ts:110)、[SourceArea.tsx:141](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/SourceArea.tsx:141)、[page.tsx:65](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/page.tsx:65) |
| P1 | **画布收不到后续节点变化。** React Flow 节点只从初始 props 建立一次，异步项目数据和新候选不能更新实际画布；节点计数却使用最新 props。外部传入的 selectedId 也未消费，来源跳转与画布选中不一致。 | [CanvasViewport.tsx:83](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/CanvasViewport.tsx:83) |
| P1 | **来源连线不存在，也未实现确认的关系结构。** 查找了实际 DOM 中不存在的节点 ID；即使修复查找，也只有一条直线，没有整块高亮的单一起点、共同主干及动作/对白分支。资产出现引用的连线没有接入。 | [SourceLines.tsx:35](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/SourceLines.tsx:35)、[WorkbenchShell.tsx:97](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/WorkbenchShell.tsx:97) |
| P1 | **成片来源跳转会离开剪辑，返回后本地编辑丢失。** JUMP_FRAGMENT 无成片例外，按片段 task 切到其他标签；FilmEditor 的轨道只存在组件内，卸载后恢复初始样例。左侧列表也没有真正承担视频资源选用，而是在主体内再造一份资源列表。 | [workspace-state.ts:114](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/workspace-state.ts:114)、[FilmEditor.tsx:31](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/FilmEditor.tsx:31) |
| P1 | **真实资产、镜头、视频被削成轻量卡片。** 适配器仅保留少量 ID、标题和 group，忽略 segments、episodes、jobs，丢弃媒体与采用版本。剧本/裁减字段只是只读 span，成片预览只是图标和名称；生产能力没有迁入新工作台。 | [project-adapter.ts:8](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/project-adapter.ts:8)、[PreviewNode.tsx:26](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/PreviewNode.tsx:26)、[FilmEditor.tsx:96](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/FilmEditor.tsx:96) |
| P1 | **属性和步骤对话未接到统一对象上下文。** 成片选中状态只在 FilmEditor 内，属性面板仍读画布状态；其他对象的“归属”可能只是名称或角色分类，来源、版本大量缺失。对话发送永久禁用，没有历史；成片分支根本没有对话入口。 | [WorkbenchShell.tsx:219](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/WorkbenchShell.tsx:219)、[ContextPanel.tsx:32](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/ContextPanel.tsx:32)、[ConversationBar.tsx:29](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/ConversationBar.tsx:29) |
| P2 | **“任务队列”只是固定阶段列表。** 之前阶段标完成、当前阶段标进行中，不能表达真实任务的失败、重试、目标与返回上下文。新工作台只在挂载时读取项目，后台任务完成不会主动同步。 | [TaskQueue.tsx:25](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/TaskQueue.tsx:25)、[page.tsx:23](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/author/workbench/page.tsx:23) |

后端[项目读取接口](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/backend/authors.py:164)已经提供 jobs、production_steps、segments、episodes 和 creative 数据。新 UI 的部分缺口是未适配这些现有字段；精确文字范围、版本迁移、对话和剪辑内核则确实仍需正式契约，不能把两种原因混在一起。

## 浏览器复现记录

对真实 3001 只做入口和公开列表读取。新工作台交互使用主目录源码副本与内存假接口，测试正文为人工编写，未登录、未修改真实项目、未提交生成任务。

1. 空测试项目导入八段正文后，左侧无预置高亮。
2. 在原文中拖选并点击“新建片段”：左侧出现一块区域，画布计数显示“1 个节点”，实际 React Flow 节点数为 **0**。
3. 点击高亮文字所在的真实行：切到剧本，节点因标签重建出现。此时属性有对象，但画布节点 `selected=false`；DOM 节点只有 `data-id=F01`，没有连线查找所需的 ID，SVG 连线数为 **0**。
4. 进入成片并选中“示例镜头·对白”：中间显示已选，右侧仍显示“未选择对象”；对话入口数为 **0**。
5. 将来源片段明确插入成片后有三个剪辑项。随后点击左侧荧光区域，标签切到剧本；再回成片只剩初始两个样例，刚插入项消失。

另用当前源码的来源模型、适配器和 reducer 做初始化检查：同一八段正文，“本会话刚导入”得到 0 个片段，重新初始化得到 3 个示例片段，其中 2 个预置“已确认”；新候选从 1 个回到 0 个；更换正文的现有动作仍保留旧候选范围；输入真实 G07 切分不会成为来源列表项。刷新问题由初始化代码检查确认，未把临时服务结束后的浏览器刷新计作成功复现。

## 首页与视觉落差

实测公开项目接口返回 HTTP 200、项目数为 0，首页空态在这次观察中对应真实空列表。没有证据把它归为接口报错或缓存问题；本次也未核实文档所述历史数据重置是否发生。

同时，[D09 主页设计](../archive/pre-rebuild/workbench/d09-project-entry-design.md)包含封面、搜索、主题切换和同项目多成片；[当前主页](/home/aoike/Project/Agent_Orchestration/workflow/Storyloom/web/app/ProjectHome.tsx:201)主要是文字卡片和单 release 入口，未达到文档列出的界面范围。

工作台已有连续原文、两种来源视图，以及非成片工作区的拖动/平移/缩放基础。阅读区仍被压在默认 288px 栏内，顶部按钮挤压标题，1280px 测试视口中“故事来源”已换行；主体重复资源栏和常驻属性面板又压缩了成片编辑空间。更关键的落差是缺少真实来源与对象关系、片段上下文和步骤对话，不能只用颜色、间距或卡片样式调整解决。

## 实施与验收为什么没有发现

[实施计划 §7.4](../archive/pre-rebuild/snapshots/main-c19639d/implementation-plan.md)明确记录 A1–E 前端完成，也明确说明持久化、对话、媒体内核未接入。其验证仅记录 `tsc --noEmit` 和 `next build`，浏览器几何与交互走查留待之后。

因此需要区分三件事：设计已经确认、前端组件已经写出、完整交互已经验收。当前发布入口采用了第二种状态，但用户需要第三种状态；已经写出的前端还存在本报告复现的阻断缺陷。构建通过只能证明代码可以编译，无法证明连线存在、节点及时更新、选择联动正确或剪辑状态保留。

## 后续实施依据

用户在原因核对后决定全面重构，原报告中的渐进修补、能力交接和旧引用迁移建议已经撤回并归档。新实施只依据[当前重构计划](../execution/implementation-plan.md)；本报告保留问题事实与复现证据，不派发开发任务。

本次交付为原因核对与复现报告，未改动应用实现和生产数据；临时测试进程已结束。
