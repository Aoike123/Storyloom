# 工作空间、剧本与关系卡

版本：2.1 · 当前任务定义。顺序与派发规则见[实施计划](../implementation-plan.md)，实际状态只见[账本](../acceptance-ledger.json)。

路径以[项目结构](../../architecture/project-structure.md)为准。任务族父卡只汇总，不派发；执行器收到下面一张单行为子卡及已冻结接口。主模型派发时收窄白名单至至多3个实现文件加直接验证，并填入准确命令/操作与负向情形。

## VIEW-01 · 新项目工作台状态容器

- 依赖：ROOT-02、BASE-01。白名单：web/features/studio/shell/studio-state.ts、web/features/studio/StudioApp.tsx、web/tests/studio/state.test.cjs。
- 实施：五职责、project/fragment/ObjectRef、来源开关/视图；资产与film项目级；业务模型不放视图reducer。
- 验收：项目隔离，后台job状态不跳标签；片段恢复正确；无旧stage或context兼容分支。
- 产品规则：R02, R06，见[产品规范](../../product/product-spec.md)。

## VIEW-02 · 受控无限画布与自由布局

- 依赖：VIEW-01、DB-03～05。白名单：web/features/studio/canvas/components/InfiniteCanvas.tsx、web/features/studio/canvas/canvas-nodes.ts、web/tests/studio/canvas.test.cjs。
- 实施：真实对象派生nodes，位置独立，受控增删/字段同步，平移/缩放/适配/拖动。
- 验收：异步读入和新对象立即出现，数量与DOM一致；拖后更新不复位；坐标不改父级/顺序/采用revision。
- 产品规则：R02, R06，见[产品规范](../../product/product-spec.md)。

## VIEW-03 · 统一选择与对象属性

- 依赖：VIEW-02、ROOT-02。白名单：web/features/studio/shell/components/ObjectInspector.tsx、web/features/studio/shell/studio-state.ts、web/features/studio/shell/object-view.ts。
- 实施：节点/来源/剪辑实例的实际ObjectRef选择，字段/归属/来源/版本/状态，空白和失效选择清空。
- 验收：节点高亮、属性、出处指同对象revision；不把group标题当归属；资源与实例选择不混淆。
- 产品规则：R02, R06，见[产品规范](../../product/product-spec.md)。

## VIEW-04 · 上下文与视图偏好恢复

- 依赖：VIEW-03、SOURCE-12。白名单：web/features/studio/shell/view-preferences.ts、web/features/studio/StudioApp.tsx、web/tests/studio/state.test.cjs。
- 实施：账号/项目/片段/步骤保存视野、阅读位置、展开与选择；schema/来源版本校验；资产/film项目范围。
- 验收：往返/刷新可恢复有效位置；坏缓存/失效对象回默认；缓存不存token、确认、采用或业务对象替身。
- 产品规则：R02, R06，见[产品规范](../../product/product-spec.md)。

## SCRIPT-01 · 剧本草稿命令

- 依赖：DB-03、SOURCE-04、BASE-02。白名单：backend/studio/scripts/service.py、backend/studio/scripts/api.py、tests/studio/test_scripts.py。
- 实施：新建/编辑一个action/dialogue对象，来源指整块fragment；稳定ID/revision、明确正式顺序。
- 验收：同片段多个对象可重读；保存不隐式采用/分镜/生成；跨项目、过期写拒绝。
- 产品规则：R05, R07, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### SCRIPT-01-a · 新建剧本草稿对象

- 唯一目标：创建真实动作/对白草稿与出处，不采用。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SCRIPT-01-b · 修改剧本草稿

- 唯一目标：只改当前草稿revision，过期保存拒绝。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## SCRIPT-02 · 采用剧本revision及影响

- 依赖：SCRIPT-01、DB-07。白名单：backend/studio/scripts/service.py、backend/studio/scripts/api.py、backend/studio/reviews/impact.py、tests/studio/test_scripts.py。
- 实施：明确采用，固定来源/依赖摘要，现有下游待更新；草稿与采用revision分开。
- 验收：仅编辑不影响已采用版本；采用新稿保留旧视频依据，不静默改旧发布。
- 产品规则：R05, R07, R11，见[产品规范](../../product/product-spec.md)。

## SCRIPT-03 · 动作/对白画布编辑

- 依赖：SCRIPT-01～02、VIEW-03。白名单：web/features/studio/script/components/ScriptNode.tsx、web/features/studio/script/components/ScriptActions.tsx、web/features/studio/script/api.ts。
- 实施：独立可拖对象、编辑/取消/保存/采用，输入焦点避开画布拖动快捷键。
- 验收：不是只读span“待填写”；刷新保存字段正确；改字段不拆原文高亮或隐式采用。
- 产品规则：R05, R07, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### SCRIPT-03-a · 编辑剧本对象UI

- 唯一目标：接草稿读写，未保存状态准确，节点更新立即同步。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SCRIPT-03-b · 采用剧本UI

- 唯一目标：明确采用具体版本及影响，不自动生成分镜。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## REL-01 · 整块高亮单起点主干出线

- 依赖：SOURCE-13、SCRIPT-03、VIEW-02。白名单：web/features/studio/canvas/components/SourceConnections.tsx、web/features/studio/source/components/SourceReader.tsx、web/features/studio/canvas/anchors.ts。
- 实施：按契约painted行/ref几何，一个起点和主干到多个action/dialogue；局部关系；离屏定位。
- 验收：跨行/长区/收栏/滚动/拖动/缩放后锚点仍贴涂色边缘；两个目标只有一个来源起点；没有不存在的DOM ID。
- 产品规则：R05, R07，见[产品规范](../../product/product-spec.md)。

## REL-02 · 资产出现引用命令

- 依赖：DB-06、SOURCE-04、ASSET-01。白名单：backend/studio/relations/service.py、backend/studio/relations/api.py、tests/studio/test_relations.py。
- 实施：source范围关联实际asset/revision，统一属主/来源/版本校验；不改资产归属、不建新故事片段。
- 验收：同asset多处出现不复制asset；提及不等于图就绪；出现范围与主片段范围各司其职。
- 产品规则：R05, R07，见[产品规范](../../product/product-spec.md)。

## REL-03 · 资产/镜头来源双向定位

- 依赖：REL-01～02、SHOT-01～02、ASSET-03。白名单：web/features/studio/canvas/components/SourceConnections.tsx、web/features/studio/assets/components/AssetNode.tsx、web/features/studio/storyboard/components/ShotNode.tsx。
- 实施：显示当前选中的出现/剧本/镜头实际引用，支持定位及返回原视野；切选择清旧网。
- 验收：共用asset定位准确版本；输入绑定不是源出处/父级；不累积全项目线，浏览不会写业务。
- 产品规则：R05, R07，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### REL-03-a · 资产出现双向定位

- 唯一目标：同资产多个出处共享身份，返回原上下文。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### REL-03-b · 镜头来源双向定位

- 唯一目标：定位准确镜头revision及片段，选择不改输入。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。
