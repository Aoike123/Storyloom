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
| 参考图、身份、服装、场景的版本变化会使下游分镜、视频和门失效 | `preproduction.invalidate_downstream_references`、`consistency.stamp` |
| 旧轮次（重做前的作品）不能作为当前制作的参考 | `asset_workflow.validate_asset_origin` |
| 视频只能引用它提交时冻结的那一版参考图 | `production.validate_references` |
| 已删除的镜头参考图步骤不会在恢复时重建付费任务 | `worker.is_removed_reference_image` |
| 付费调用需要按类型开启 + 共享池预留；被供应商拒绝的调用会退还预留 | `providers.reserve_call`、`model_access.release_public_call`、`provider_usage.finish` |
| 共享池的每日预算在 API 与 worker 两个进程间串行比较，不会双花 | `model_access._lock_pool_row` |
| 只有反向代理网段可以设置 `X-Forwarded-For` | `docker/api.Dockerfile`、`compose.production.yaml` 的 `STORYLOOM_TRUSTED_PROXY` |
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

## 额度

- 计价：文本 `BEANS_LLM_COST`、生图 `BEANS_IMAGE_COST`、视频 `BEANS_VIDEO_COST_PER_SECOND × 秒数`；每个账号只赠送一次 `BEANS_INITIAL_GRANT`。
- 提交时才扣豆，排队不扣；供应商拒绝（HTTP 4xx）会退回豆子，超时或 5xx 等结果不确定的调用保留扣费，不会自动重发。
- 切片节点会按账号剩余豆子限制整片镜头总数；不足以完成一个短片时会直接说明，而不是拍到一半停下。
- 登录账号的豆子花完后，页面会提示改用自带 API Key；未登录访客必须先登录或用自带 Key，二者都不具备时只会得到说明，不会发起付费调用。
