# 成片卡

版本：2.1 · 当前任务定义。顺序与派发规则见[实施计划](../implementation-plan.md)，实际状态只见[账本](../acceptance-ledger.json)。

路径以[项目结构](../../architecture/project-structure.md)为准。任务族父卡只汇总，不派发；执行器收到下面一张单行为子卡及已冻结接口。主模型派发时收窄白名单至至多3个实现文件加直接验证，并填入准确命令/操作与负向情形。

## EDIT-01 · 项目剪辑草稿读写

- 依赖：DB-10、BASE-02。白名单：backend/studio/edits/service.py、backend/studio/edits/api.py、tests/studio/test_edits.py。
- 实施：空草稿/读取/聚合版本CAS与command幂等，毫秒数据。
- 验收：重开能读准确草稿，无默认两样例或96秒；并发409无半次写，不靠localStorage存业务。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。

## EDIT-02 · 实际视频资源查询与左侧浏览

- 依赖：JOB-09、SOURCE-12、EDIT-01。白名单：backend/studio/storyboard/resources.py、backend/studio/storyboard/api.py、web/features/studio/film/components/FilmResources.tsx、web/features/studio/source/components/SourcePanel.tsx；按查询/浏览子卡收窄。
- 实施：按真实fragment/shot/clip revision分组，仅可用审核产物；选资源只预览/定位。
- 验收：文字候选不冒充视频；film来源点击不离开剪辑，不替换时间轴；中间无重复资源栏。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### EDIT-02-a · 可用视频资源查询

- 唯一目标：只查准确已审核采用且有真实文件的clip版本。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-02-b · 左侧成片资源浏览

- 唯一目标：点击只预览，保留时间轴，中部无重复资源区。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## EDIT-03 · 明确插入独立实例

- 依赖：EDIT-02。白名单：backend/studio/edits/service.py、web/features/studio/film/components/FilmResources.tsx、tests/studio/test_edits.py。
- 实施：核对真实源版本/入出点，服务端分配instance ID；冻结完整出处。
- 验收：同clip两次ID不同且源相同；重复command无第三次；未审核/缺媒体/跨项目拒绝。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。

## EDIT-04 · 实例重排和删除

- 依赖：EDIT-03。白名单：backend/studio/edits/service.py、web/features/studio/film/components/TimelineActions.tsx、tests/studio/test_edits.py。
- 实施：重排、删除分别派发命令，操作独立instance和track/timeline位置；原子保存。
- 验收：删一实例仍可再取源资源；不删除clip或改镜头制作顺序；失败/过期可恢复。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### EDIT-04-a · 重排实例

- 唯一目标：只改独立instance时间轴位置，源镜头顺序不变。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-04-b · 删除实例

- 唯一目标：只删一个实例，同源另实例和clip保留。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## EDIT-05 · 实例时间、速度与轨道编辑

- 依赖：EDIT-03、ROOT-03。白名单：backend/studio/edits/service.py、web/features/studio/film/components/InstanceProperties.tsx、tests/studio/test_edits.py。
- 实施：按内核支持范围逐操作派发，入出点/位置/速度/声画参数统一验证，不加未支持假效果控件。
- 验收：非法时间拒绝，预览/保存/刷新一致；属性对应实际实例，源版本不随编辑改变。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### EDIT-05-a · 裁切实例入出点

- 唯一目标：验证合法源区间，保存后真实预览一致。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-05-b · 移动时间轴位置

- 唯一目标：整数时间保存，不改变源in/out。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-05-c · 改变速度

- 唯一目标：仅ROOT-03通过的支持范围，预览/导出一致。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-05-d · 新建轨道

- 唯一目标：只在内核支持时建明确轨道，不改源媒体。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-05-e · 实例移轨

- 唯一目标：只改实例track，拒绝不合法重叠/类型。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-05-f · 轨道静音

- 唯一目标：只改轨道静音，预览/导出同manifest一致。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-05-g · 声音参数

- 唯一目标：仅已冻结音量/静音等参数，不新增假效果。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## EDIT-06 · 受控全尺寸编辑器及恢复

- 依赖：EDIT-04～05、VIEW-03～04。白名单：web/features/studio/film/components/FilmEditor.tsx、web/features/studio/film/edit-session.ts、web/features/studio/film/api.ts。
- 实施：项目唯一编辑会话、持久化命令队列、保存/失败/冲突；属性按需，底部留对话空间。
- 验收：插入→切片段/标签→回来→刷新不丢；A/B隔离；实例选择同步属性；无自由画布卡片层。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。

## EDIT-07 · 同manifest真实媒体预览

- 依赖：ROOT-03、EDIT-06、MEDIA-02。白名单：web/features/studio/film/preview-adapter.ts、web/features/studio/film/components/FilmMonitor.tsx及对应媒体测试。
- 实施：实际duration/seek/切点/声画/支持操作消费标准manifest；资源preview与时间轴预览分开。
- 验收：本地时间码/音频证明实际播放；不是图标/定时器；选资源预览不清编辑；缺媒体准确报错。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。

## EDIT-08 · 确认不可变剪辑版本

- 依赖：EDIT-07、DB-07。白名单：backend/studio/edits/service.py、web/features/studio/film/components/ConfirmEdit.tsx、tests/studio/test_edits.py。
- 实施：检查输入、审核/采用及规格，冻结ConfirmedEdit与digest；后续草稿创建新revision。
- 验收：修改草稿不改变确认版；过期/缺文件不能确认；审核视频和确认剪辑不合并。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。

## EDIT-09 · 新同版导出处理器

- 依赖：EDIT-08、JOB-03、ROOT-03。白名单：backend/studio/jobs/handlers/export.py、backend/infrastructure/media/export_adapter.py、tests/studio/test_export_jobs.py。
- 实施：只消费ConfirmedEdit和output_spec，实际导出校验/摘要；失败可原版本重试。
- 验收：可打开真实文件，切点/时长/声音与预览一致；重试不重生镜头/重新采用草稿；安全参数不经shell执行文本。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。

## EDIT-10 · 新发布和观看

- 依赖：EDIT-09、MEDIA-02。白名单：backend/studio/publishing/service.py、backend/studio/publishing/api.py、web/app/watch/[releaseId]/page.tsx、web/features/viewer/ReleaseViewer.tsx；逐子卡收窄。
- 实施：发布固定同ConfirmedEdit/规格/文件；新观看查询新Release，发布与public分开。
- 验收：观看真实确认产物；后续草稿不改旧发布；仅公开白名单，无旧release格式适配，导出失败可独立恢复。
- 产品规则：R09, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### EDIT-10-a · 发布固定剪辑版本

- 唯一目标：固定同ConfirmedEdit/规格/文件，不重新采用草稿。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-10-b · 新发布查询API

- 唯一目标：公开白名单/属主查询分开，无旧release格式适配。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### EDIT-10-c · 新观看页面

- 唯一目标：播放新Release真实产物，失败恢复准确。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。
