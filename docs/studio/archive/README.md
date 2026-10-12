# 历史档案

此目录只保留决策和实现背景，**所有内容（包括旧后端workflow、节点绑定与阶段管线）已退出当前设计和新实施依据**。返回[当前入口](../README.md)。

```text
pre-rebuild/
├── workbench/                 重构前方向、规范、F/D框架和进度记录
├── reference/                 旧制作/播放/存储资料、原README与作者流程
└── snapshots/main-c19639d/    仅保留有实质差异的主目录文档版本
```

workbench中的资料保留本工作树先前未提交的设计更新；D09由主目录读取。主目录截至整理时的提交为c19639d，文档差异单独保存。文件内旧状态声明仅描述当时版本，包括“框架已完成”“前端A1–E完成”和旧迁移/清库方案，不能用作当前应用能力或操作授权。

归档正文尽量保留，只加停用标记并修正移动后的文档链接；已确认HTML稿保留在当前prototypes目录。新计划没有恢复旧数据清空、兼容迁移、双入口或旧阶段管线任务。

如需引用历史事实，必须说明基线与时间，并引用具体文件；执行器派发上下文默认不含此目录。可提取基础设施也须重新核对实际代码，不能因旧文档写过“已验证”直接纳入新应用。

## 2026-10-13 精简

删除 snapshots 中仅排版或相对链接不同的六份副本（canvas-design-direction、design-spec、design-task-skeleton、d09-project-entry-design、previous-README、previous-AUTHOR_WORKFLOW）；引用已指向 workbench/reference 的保留版本。三个有差异的快照继续保留。

根目录 AUTHOR_WORKFLOW 导航页与 2026-09-12 的 VERIFICATION 旧验收记录已删除；当前流程与实际验收统一从工作台入口查询。旧 accept-ui.mjs、benchmark_models.py、reset-local.py 脚本已删除：分别只验旧 author 页面、使用旧数据库进行模型试跑、清空旧 records/tasks 与媒体，均不参与新 Studio 验收。历史正文中的脚本命令仅用于追溯，不能执行；删除内容可从本次变更之前的 Git 历史恢复。

保留 media_kernel_poc.py（当前媒体契约的参考实现）、start.ps1/stop.ps1（待旧栈正式退役时一并处置）及 vendor_node_skills.py（现存第三方素材维护）；这些旧栈工具不用于新 Studio 启动。
