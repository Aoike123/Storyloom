# 制作流程的安全不变量

这份文档记录流程中**由代码保证**的规则，以及哪些判断仍然依赖模型文本。目的是让验收者能区分"代码已经锁住"和"只是提示词里写了"。

## 由代码保证

| 不变量 | 实现位置 |
| --- | --- |
| 模型只能使用已确认的素材 ID；未知、重复、缺场景、人物与服装不匹配都会失败 | `preproduction.validate_board`、`asset_workflow.validate_shot_identities` |
| 镜号按整片时间轴连续，片段不能各自从 S01 重新开始；错号会被退回重做，不会被静默改写 | `segments.shot_ids_for`、`segments.merge_chunks`、`director.segment_board` |
| 跨片段承接只能引用调用方给出的 `segment.referenceable_shot_ids`，其它片段范围内的编号一律报错 | `segments.earlier_shot_ids`、`director.check_board(external_ids=…)` |
| 模型自己写的原文引文会被核对；原文中查不到即拒绝，属于其它 P 编号则改绑并记录为代码校正 | `director.bind_sources(..., content=…)` |
| 每个镜头必须绑定 1–9 张已审核参考图；超限要求拆镜，不会丢弃多余参考图 | `preproduction.validate_board`、`production.reviewed_references` |
| 角色 → 服装 → 环境对所有角色都跑完：每个角色至少一条服装记录，不着衣物的天然体表用空衣服模式（`mode=bare`），人类与任何带人类身体分区的角色（如兽首人身）都不能用它顶替服装 | `asset_sheets.AssetSheetPlan`、`visual_specs.CostumeSheet` |
| 身份图的遮盖按解剖分区决定：人类躯干、臂手、腿足始终以中性基础短装覆盖，只有无人类分区的身体才展示自然体表；否则会要求裸露的人类身体并被供应商按违规内容拒绝（HTTP 451） | `asset_sheets.human_body_regions`、`asset_sheets.frame` |
| 空衣服模式不生成服装图，也不会被绑进分镜的服装参考；分镜契约用 `bare_characters` 明确告知模型 | `creative.run_design`、`preproduction.storyboard_asset_contract` |
| 参考图、身份、服装、场景的版本变化会使下游分镜、视频和门失效 | `preproduction.invalidate_downstream_references`、`consistency.stamp` |
| 旧轮次（重做前的作品）不能作为当前制作的参考 | `asset_workflow.validate_asset_origin` |
| 视频只能引用它提交时冻结的那一版参考图 | `production.validate_references` |
| 已删除的镜头参考图步骤不会在恢复时重建付费任务 | `worker.is_removed_reference_image` |
| 付费调用需要按类型开启 + 共享池预留；被供应商拒绝的调用会退还预留 | `providers.reserve_call`、`model_access.release_public_call`、`provider_usage.finish` |
| 共享池的每日预算在 API 与 worker 两个进程间串行比较，不会双花 | `model_access._lock_pool_row` |
| 只有反向代理网段可以设置 `X-Forwarded-For` | `docker/api.Dockerfile`、`compose.production.yaml` 的 `STORYLOOM_TRUSTED_PROXY` |
| 作品按付款方归属：账号看自己的作品，用自己的 Key 的访客按会话隔离；别人的作品按 id 也读不到 | `authors.get_work`、`authors.work_id_for`、`model_access.current_actor_id` |
| 未归属的旧作品由第一个登录的账号接手一次，此后专属于该账号 | `authors.open_story`、`author_project_claimed` 审计 |
| 面板显示已录入的自带 Key（数量、尾号、有效期），但服务端从不回传完整 Key | `model_access.own_key_summary` |
| 未完成任务可以改由新的付款方继续，避免"刚填了 Key 却还在用旧付款方" | `production_nodes.adopt_current_payer` |
| 每 IP 请求上限、全局并发任务上限、容器内存与 CPU 上限 | `public_limits.py`、`db.enforce_public_task_capacity`、`compose.production.yaml` |
| 追加型历史（事件、分镜历史、重做历史、节点调用轨迹）有固定上限 | `db.bounded`、`skill_runtime.TRACE_LIMIT` |

## 由代码校正，但会在界面上标明

模型原稿可能漏绑或错绑。代码会按确定映射修复，并把这些修复记入 `board_repairs`：

- 同一镜重复的参考图去重；
- 画面文字里点名的已确认角色自动补绑其身份图与服装；
- 镜头地点能唯一匹配已批准场景时补上场景；
- 完整绑定超过每镜 9 张时，用本地图像工具把同一角色的身份图与服装图拼成一张参考板（不调用任何生图或改图接口）；
- 按"承接 Sxx"文字补上结构化铺垫镜号。

制作页会显示"分镜参考经过 N 处代码校正"，并逐条列出。**这些不是模型自己写对的结果**，审片时应重点看对应镜头。

## 仍然依赖模型判断

- 剧情因果、人物动机、镜头审美、穿帮判断：由 `storyboard_review` 复核节点给出文字预审，没有独立裁判。
- 视觉一致性（人脸、服装、画风是否真的对上）：**没有自动验收**，依赖作者审片。
- 原文是否被"合理地"改编：代码只核对引文存在与编号连续，不判断改编是否忠于原意。

## 失败与恢复

- 每个失败任务都会得到一个错误编号（如 `E-1A2B3C`），完整堆栈只保存在服务端。
- 本地工作台可以通过 `GET /api/diagnostics/{编号}` 查看详情；公开演示模式返回 404，避免泄露路径与供应商响应。
- 校验失败会在节点内自动重试（默认 3 次），并把具体原因回灌给模型。
- 制作节点失败后会**自动恢复一次**（复用已通过校验的成果）；再次失败才停下来等人处理。额度不足、未配置、供应商暂停等不会自动重试。
- 分镜重试按片段进行：只有出错的片段重新生成，已通过校验的片段沿用已保存结果。
- 分镜专业预审（`storyboard_review`）未通过属于**可自动恢复**的失败：预审给出的具体问题会作为本轮必须修正的约束交回分镜模型，整片重做一次；评审结论（连续性、戏剧逻辑、可剪辑性、制作可行性）一并附上。预审是整片判断，因此这次重做不沿用任何已通过校验的片段。再次未通过才停下等人。
- 预审重做有上限（`creative.REVIEW_RETRY_ATTEMPTS = 2`）。达到上限后进入**降级继续**：成片照常制作，预审问题记入 `storyboard_review_degraded` 并写审计记录，制作页显示醒目提示，作者审片时能看到。纯文字预审没有客观真值，这条路径保证它不会把一部可以渲染的片子永久卡住。
- 自动恢复的判定只拦截需要人工介入的原因（额度不足、未配置、未开启、模型使用权限、供应商今日已暂停、HTTP 401/402/403、Key 缺失）。判定必须精确：曾经用过过宽的「暂停」一词，把「分镜生成暂停」这类可恢复消息也一起挡掉了。
- 失败消息只保留**一个**错误编号；同一个原因被上层任务再次上报时替换旧编号，不叠加。

## 额度

- 计价：文本 `BEANS_LLM_COST`、生图 `BEANS_IMAGE_COST`、视频 `BEANS_VIDEO_COST_PER_SECOND × 秒数`；每个账号只赠送一次 `BEANS_INITIAL_GRANT`。
- 提交时才扣豆，排队不扣；供应商拒绝（HTTP 4xx）会退回豆子，超时或 5xx 等结果不确定的调用保留扣费，不会自动重发。
- 切片节点会按账号剩余豆子限制整片镜头总数；不足以完成一个短片时会直接说明，而不是拍到一半停下。
- 登录账号的豆子花完后，页面会提示改用自带 API Key；未登录访客必须先登录或用自带 Key，二者都不具备时只会得到说明，不会发起付费调用。
