# 任务与媒体卡

版本：2.1 · 当前任务定义。顺序与派发规则见[实施计划](../implementation-plan.md)，实际状态只见[账本](../acceptance-ledger.json)。

路径以[项目结构](../../architecture/project-structure.md)为准。任务族父卡只汇总，不派发；执行器收到下面一张单行为子卡及已冻结接口。主模型派发时收窄白名单至至多3个实现文件加直接验证，并填入准确命令/操作与负向情形。

## MEDIA-01 · 私有媒体落盘与完整性

- 依赖：DB-10、ROOT-03。白名单：backend/studio/media/store.py、backend/infrastructure/media/probe.py、tests/studio/test_media.py。
- 实施：原子落盘、校验/probe、摘要与MediaArtifact，受控路径/类型/大小；复用批准的纯工具。
- 验收：真实文件可打开，损坏/越界路径拒绝；私有文件不直接进旧公开static目录；写失败无伪就绪记录。
- 产品规则：R09, R12，见[产品规范](../../product/product-spec.md)。

## MEDIA-02 · 属主媒体读取与公开产物流

- 依赖：MEDIA-01、SOURCE-01。白名单：backend/studio/media/api.py、backend/studio/media/stream.py、tests/studio/test_media.py。
- 实施：私有stream严格属主、Range请求；public仅按Release固定文件白名单访问。
- 验收：非属主不能猜ID读私有图/视频；公开观看不泄漏原文/过程资产；seek可用且无过度全量重传。
- 产品规则：R09, R12，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### MEDIA-02-a · 私有媒体流

- 唯一目标：严格属主与Range请求；猜ID不读他人文件。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### MEDIA-02-b · 公开发布媒体流

- 唯一目标：仅访问该Release固定文件白名单，不开放过程媒体。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## JOB-01 · 无旧业务依赖的供应商client

- 依赖：ROOT-04、DB-08、MEDIA-01。白名单：backend/infrastructure/providers/client.py、backend/infrastructure/providers/protocols.py、tests/studio/test_provider_adapter.py。
- 实施：按冻结协议抽出纯请求/轮询/错误元数据，调用结果归新job，不读写旧Task/Record。
- 验收：假HTTP覆盖文本/图片/视频协议成功与失败；请求实际prompt/model/inputs可追溯；没有真实收费调用。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-02 · 冻结目标并幂等提交任务

- 依赖：JOB-01、DB-07～08。白名单：backend/studio/jobs/service.py、backend/studio/jobs/api.py、tests/studio/test_jobs.py。
- 实施：服务端解析明确target/revisions/scope，检查就绪/成本确认，存input_digest与command_id。
- 验收：同command同结果，未来新增对象不扩大任务；过期/缺输入拒绝；切片段不重新归属。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-03 · 新worker租约执行与恢复

- 依赖：JOB-02。白名单：backend/studio/jobs/runner.py、backend/studio/jobs/worker_main.py、tests/studio/test_job_runner.py。
- 实施：claim/lease/token竞争、持久尝试与供应商request ID；处理器按job kind注册；崩溃恢复不盲目重发收费请求。
- 验收：两个worker不会同时执行同attempt；过期claim不能写当前结果；重启可继续已知轮询；不调用旧author_flow。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-04 · 真实事件、SSE与前端合并

- 依赖：JOB-03、VIEW-01。白名单：backend/studio/jobs/events_api.py、web/features/studio/jobs/job-feed.ts、web/tests/studio/job-feed.test.cjs。
- 实施：单调事件游标、重连/补读、目标结果重取；project/revision/序号隔离。
- 验收：旧running不覆盖completed，A慢事件不进B；完成不抢标签；断连可恢复，状态不是固定阶段列表。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-05 · 失败重试命令

- 依赖：JOB-03。白名单：backend/studio/jobs/service.py、backend/studio/jobs/api.py、tests/studio/test_jobs.py。
- 实施：原冻结输入的新attempt、策略错误需明确修正为新输入任务；已成功不重复生成。
- 验收：重试可追溯原尝试，重复命令幂等；其他成功素材不丢；无active并发重试。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-06 · 取消与迟到结果

- 依赖：JOB-03。白名单：backend/studio/jobs/service.py、backend/studio/jobs/api.py、backend/studio/jobs/runner.py、tests/studio/test_jobs.py。
- 实施：取消请求/供应商能力差异/无法取消的迟到结果按冻结状态表处理，结果保留但不采用。
- 验收：切标签不是取消；取消完成不自动复活任务；迟到结果不覆盖选中版本/焦点。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-07 · 显式重做新输入任务

- 依赖：JOB-05、DB-07。白名单：backend/studio/jobs/service.py、web/features/studio/jobs/components/RedoReview.tsx、tests/studio/test_jobs.py。
- 实施：预览具体目标/影响，明确确认后新输入digest与任务；保留旧产物。
- 验收：浏览/编辑不会调用重做；过期预览拒绝；旧视频和已发布确认版本不被删除。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-08 · 图片生成处理器

- 依赖：JOB-03、ASSET-01、MEDIA-01。白名单：backend/studio/jobs/handlers/image.py、backend/studio/assets/service.py、tests/studio/test_image_jobs.py。
- 实施：冻结资产规格/参考版本，真实图片提交/接收/校验，形成待审核AssetRevision。
- 验收：可用媒体实际落盘，失败有原因/恢复；提交成功不等完成，完成不等人工采用；输入及出处保留。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-09 · 视频生成与逐版本审核

- 依赖：JOB-03、SHOT-01、ASSET-03、MEDIA-01。白名单：backend/studio/jobs/handlers/video.py、backend/studio/reviews/service.py、backend/studio/reviews/api.py、tests/studio/test_video_jobs.py；按生成/审核子卡收窄。
- 实施：冻结采用分镜和审核素材输入、实际时长，接clip结果与视频审核；不重新拼未展示的“最新版本”。
- 验收：镜头就绪门槛真实；clip属于原shot/revision；媒体/审核可重读；作者未采用不会自动进资源就绪列表。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### JOB-09-a · 视频生成处理器

- 唯一目标：冻结准确分镜/资产输入，落盘clip仍归原revision。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-09-b · 视频审核采用命令

- 唯一目标：准确clip版本人工审核/采用，未采用不成为就绪资源。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## JOB-11 · 剧本生成处理器

- 依赖：JOB-03、SCRIPT-01、SOURCE-04。白名单：backend/studio/jobs/handlers/script.py、backend/studio/scripts/service.py、tests/studio/test_script_jobs.py。
- 实施：冻结准确片段/来源，生成结构化动作与对白草稿及出处；校验后记录为未采用版本。
- 验收：错误出处/跨项目目标拒绝；结果归发起revision，未自动确认/采用/生成视频；失败可以按既定任务协议恢复。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-12 · 分镜生成处理器

- 依赖：JOB-03、SHOT-01、SCRIPT-02。白名单：backend/studio/jobs/handlers/storyboard.py、backend/studio/storyboard/service.py、tests/studio/test_storyboard_jobs.py。
- 实施：基于固定采用剧本/资产版本生成场与镜头方案、实际顺序、出处与输入需求；作为待审核草稿。
- 验收：缺失/错误输入不伪成功，镜头唯一归属；迟到草稿不覆盖当前采用计划；生成完成不自动发视频任务。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。

## JOB-10 · 生成操作与项目任务队列

- 依赖：JOB-04～09、JOB-11～12、VIEW-03。白名单：web/features/studio/jobs/components/GenerationActions.tsx、web/features/studio/jobs/components/TaskQueue.tsx、web/features/studio/jobs/api.ts。
- 实施：按步骤明确目标/输入/成本/失败操作；实际队列含target定位；逐种动作派发，不一卡做所有表单。
- 验收：生成/retry/cancel/redo分别走对应命令；queue返回不重复发任务；状态及结果可定位原对象，无agent名字强制标签。
- 产品规则：R08, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### JOB-10-a · 图片生成UI

- 唯一目标：展示目标/输入/成本后明确建任务。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-10-b · 视频生成UI

- 唯一目标：具体镜头门槛通过才发命令。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-10-c · 剧本生成UI

- 唯一目标：明确片段/版本，结果待采用。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-10-d · 分镜生成UI

- 唯一目标：明确采用剧本/范围，结果待审核。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-10-e · 失败重试UI

- 唯一目标：接retry，先核实不确定原请求。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-10-f · 取消任务UI

- 唯一目标：只接cancel，迟到结果保留原任务不抢焦点。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-10-g · 重做任务UI

- 唯一目标：只接新输入redo，展示影响后明确提交。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### JOB-10-h · 真实队列与定位UI

- 唯一目标：合并事件/进度并回原位置，不重复提交。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。
