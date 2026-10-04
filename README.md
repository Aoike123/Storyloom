# 叙间 Storyloom

从文字故事制作剧本、分镜、资产、视频与成片的创作工作台。

## 当前方向与状态

本轮按已确认设计**全面重构，旧产品实现退役**。当前应用代码仍有新旧工作台混用问题；文档整理、问题核对和只读依赖盘点已完成，新应用实现尚未开始。应用能力以主模型的实际验收记录为准。

**设计与开发统一从 [工作台文档入口](docs/studio/README.md) 开始。**

- 看产品与交互：[产品规范](docs/studio/product/product-spec.md)、[已确认交互稿](docs/studio/prototypes/source-studio.html)。
- 看前后端结构：[项目结构与模块边界](docs/studio/architecture/project-structure.md)。
- 执行重构：[实施计划](docs/studio/execution/implementation-plan.md)、[验收账本](docs/studio/execution/acceptance-ledger.json)。

左侧提供连续原文与动态故事片段视图；整块荧光区域对应片段。切分、剧本、分镜、资产使用无限画布；成片采用全尺寸剪辑组件，左侧就是资源区。属性跟随所选对象，每步骤底部都有自然语言对话入口。

## 开发说明

仓库包含 Next.js/React 前端和 FastAPI/Python 后端。新目录、路由、数据模型及运行方式以重构契约为目标，不能将目标结构误读为现有功能。

实施前先完成 ROOT-01：固定代码目录、基线、隔离数据与预览端口。新启动说明在运行签收后更新；原有启动命令和作者流程已放入[历史资料](docs/studio/archive/README.md)，不再作为新产品说明。

重构过程不清空已有数据库或批量删除媒体；应用代码退役和存储处置分别处理。验证使用隔离数据、假供应商及本地媒体，生成操作的真实接入单独验收。

安全说明见 [SECURITY.md](SECURITY.md)。
