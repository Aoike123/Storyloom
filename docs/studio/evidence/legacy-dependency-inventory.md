# 旧产品退役依赖盘点

日期：2026-10-04。ROOT-05只读交付，主模型验收。盘点主目录基线为d43b51f；文档整理时主模型在c19639d复核关键引用，后者提交说明为代码格式整理。**只证明依赖盘点完成，不证明应用重构完成。** 删除时须按ROOT-01冻结的实际HEAD复核名单。

## 新旧混用的依赖链

| 入口/模块 | 当前依赖 | 重构处理 |
| --- | --- | --- |
| 首页ProjectHome | `/api/author/projects`，创建/继续进入`/author/workbench` | 新projects入口消费新API，删除旧组件/路由引用 |
| 旧author与workbench | 共用旧项目格式，前者承载制作，后者主要导入/浏览 | 独立新StudioApp，不移动旧组件伪装新结构 |
| 旧watch | `/api/author/releases/{id}`与旧release格式 | 新Release/公开查询/观看路径 |
| backend/app.py | auth、author、creative、director、preproduction、production/reader、reader_branch、storage、consistency | 保留经核对的通用启动/中间件，重写新路由装配 |
| authors.py | creative/director/production_nodes/story_sources/skill_runtime/video_storage等 | 解除新模块引用后退役，不能当独立UI删除 |
| 旧worker | Record/Task阶段、production_nodes、provider、task_activity、diagnostics等 | 新StudioJob/runner/handler独立状态与入口 |
| providers/image_provider | 协议调用同时记录旧任务活动、调用usage和生成请求 | 只提取纯协议/配置，重新接新attempt/event/媒体登记 |
| video_storage.py | Record查询、素材/视频/选用/发布格式 | 新MediaArtifact/ConfirmedEdit/Release；不整文件复用 |

原文旧P引用实际来自固定字符窗口，并非浏览器原生段落。新范围直接使用新字符/来源版本契约，盘点不产生P/G映射或旧项目迁移任务。

## 可提取基础设施

- 数据库连接、SQLAlchemy Base与账号身份：从旧db模块分离，旧Record/Task不作为新业务权威。
- 严格HTTP用户/属主鉴权：核对`require_user`与账号会话；不能将旧`assert_owner`内部请求上下文的后台例外直接当新HTTP规则。
- 配置/纯供应商协议：提取前核对实际旧写入，重新注入新执行记录和错误协议。
- 纯媒体文件工具：探测、裁切等可复用经验证部分；私有文件与公开发布访问重新界定。
- 启动生命周期、origin guard、身份上下文、通用健康检查：保持通用能力，旧worker heartbeat/Record统计单独退役。

## 删除顺序与边界

本文是模块分类与依赖证据，**不是逐文件删除白名单**。RETIRE-01由主模型依据实际实施HEAD另行冻结文件清单、动作、引用方及复验范围；未冻结不能派发退役操作。

1. ROOT/BASE确定新schema、端口与基础设施白名单，建立独立新命名空间。
2. 新来源/制作/资产/任务/对话/剪辑/发布形成真实路径，并通过GATE-01至05。
3. 按RETIRE-02删除旧前端入口、组件和专用样式；新页面不再请求旧API。
4. 按RETIRE-03解除并删除旧后端管线/路由/worker，重新装配新启动入口。
5. 按RETIRE-04清理旧专用测试和工具，更新真实运行说明；GATE-06核对实际checkout与页面。

清单用于代码退役和纯基础设施提取，没有数据库清空、媒体删除、业务交接或兼容层任务。

复核依据：主模型只读检查主目录`backend/app.py`的import/include_router、`backend/auth.py`的require_user/assert_owner、`backend/providers.py`的task_activity/provider_usage/save_generation_request、`backend/image_provider.py`及`backend/video_storage.py`的旧db耦合；检查首页、workbench、watch的API/导航引用。详细原问题见[实现核对报告](implementation-audit-2026-10-04.md)。
