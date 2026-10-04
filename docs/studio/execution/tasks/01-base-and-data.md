# 基础与数据卡

版本：2.1 · 当前任务定义。顺序与派发规则见[实施计划](../implementation-plan.md)，实际状态只见[账本](../acceptance-ledger.json)。

路径以[项目结构](../../architecture/project-structure.md)为准。任务族父卡只汇总，不派发；执行器收到下面一张单行为子卡及已冻结接口。主模型派发时收窄白名单至至多3个实现文件加直接验证，并填入准确命令/操作与负向情形。

## BASE-01 · 新前后端命名空间骨架

- 依赖：ROOT-02。白名单：web/app/studio/[projectId]/page.tsx、web/features/studio/StudioApp.tsx、backend/studio/router.py、backend/studio/app_factory.py；分前/后端子卡，注册接入由主模型集成。
- 实施：接收projectId，建立新应用和API入口/准确空态，样式作用域独立；不挂旧组件。
- 验收：隔离预览能打开新路由，缺/错projectId状态明确；新UI不请求旧API，不向正常项目注入fixture。
- 产品规则：R01, R06, R12，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### BASE-01-a · 新工作台路由与空态

- 唯一目标：只建立新路由/StudioApp，缺projectId不进入示例项目。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### BASE-01-b · 新API装配入口

- 唯一目标：只建立新router/app_factory，隔离app不注册旧业务路由。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## BASE-02 · 新API客户端和错误协议

- 依赖：BASE-01。白名单：web/shared/api/client.ts、web/shared/api/types.ts、web/tests/studio/api-client.test.cjs。
- 实施：按冻结schema处理认证、JSON、400/401/403/409/断网、command_id及expected_revision；失败不转成功空数组。
- 验收：错误保留code/冲突目标；重复命令不新生成command_id；旧项目慢响应不能被当新项目数据。
- 产品规则：R01, R06, R12，见[产品规范](../../product/product-spec.md)。

## BASE-03 · 分离通用数据库、账号与配置基础设施

- 依赖：ROOT-02。白名单：backend/core/db.py、backend/core/accounts.py、backend/core/auth.py、backend/core/config.py及直接相关tests；逐子卡收窄，接入由主模型集成。
- 实施：按ROOT-02白名单分别提取连接/Base、账号模型、严格鉴权、纯配置读取；新core不import旧Record/Task/业务管线。
- 验收：隔离新模型可建表，账号会话/密码校验仍正确；非属主访问被拒绝；没有内部context绕过HTTP身份，没有复制真实配置/生产数据库。
- 产品规则：R01, R06, R12，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### BASE-03-a · 数据库连接与Base

- 唯一目标：提取纯连接/Base，新模块不加载旧业务表。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### BASE-03-b · 账号模型

- 唯一目标：分离账号身份模型，保留有效密码/会话字段语义。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### BASE-03-c · 严格鉴权

- 唯一目标：新HTTP接口使用严格用户和属主校验；匿名/跨属主拒绝。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### BASE-03-d · 纯配置读取

- 唯一目标：分离配置读取，不读真实密钥做测试，不import业务。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## DB-01 · 项目与不可变来源表

- 依赖：ROOT-02、BASE-03。白名单：backend/studio/projects/models.py、backend/studio/sources/models.py、tests/studio/test_source_models.py。
- 实施：StudioProject/StoryRevision及属主、不可变内容/hash/版本关系；使用新表名。
- 验收：同项目多来源版本可区分；FK/唯一约束生效；读取不建假来源，未对旧表回填/清空。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-02 · 范围集合与片段版本表

- 依赖：DB-01。白名单：backend/studio/sources/fragment_models.py、tests/studio/test_fragment_models.py。
- 实施：RangeSet、Fragment/FragmentRevision、状态和predecessor；确定集合CAS写入点。
- 验收：改名/边界可保留实体身份，拆合可保存继承；候选/确认与来源revision明确；不是画布节点JSON。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-03 · 剧本对象与版本表

- 依赖：DB-02。白名单：backend/studio/scripts/models.py、tests/studio/test_script_models.py。
- 实施：动作/对白对象及片段归属、来源、正式顺序和草稿/采用revision。
- 验收：同片段至少两个不同对象可保存；版本不复制业务归属；草稿不等于采用。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-04 · 场与镜头版本表

- 依赖：DB-03。白名单：backend/studio/storyboard/models.py、tests/studio/test_shot_models.py。
- 实施：Scene/Shot/ShotRevision，唯一scene_id、排序、采用剧本revision与规格。
- 验收：镜头不能有两份父级；坐标不在正式排序字段；错误外键/跨项目引用拒绝。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-05 · 资产和并存版本表

- 依赖：DB-01。白名单：backend/studio/assets/models.py、tests/studio/test_asset_models.py。
- 实施：人物/服装/场景等资产身份与规格/媒体revision；服装的人物归属。
- 验收：同资产新版本不新建身份；共用资产不属于某个片段副本；人物/服装关系一致。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-06 · 来源、出现、实际输入关系表

- 依赖：DB-02～05。白名单：backend/studio/relations/models.py、tests/studio/test_relation_models.py。
- 实施：SourceRelation/AssetOccurrence/AssetBinding；准确source/object/asset revision与用途/作用对象。
- 验收：出现、归属、生成输入分别可查询；重复出现能关联同资产；不另存可写使用计数。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-07 · 审核和变更预览表

- 依赖：DB-02～06。白名单：backend/studio/reviews/models.py、tests/studio/test_review_models.py。
- 实施：ReviewDecision、ChangePreview固定目标版本/依赖、基准及影响项。
- 验收：审核能区分目标revision，preview不是隐式提交；旧审核不能给新revision自动过关。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-08 · 新任务/尝试/事件表

- 依赖：ROOT-04、DB-01。白名单：backend/studio/jobs/models.py、tests/studio/test_job_models.py。
- 实施：StudioJob/JobAttempt/JobEvent、幂等键、冻结输入、claim token/lease、供应商请求ID与单调事件。
- 验收：新任务不写旧Task；一个job可追溯多个attempt；输入快照与状态不是由前端stage推导。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-09 · 会话/消息/提案表

- 依赖：ROOT-04、DB-08。白名单：backend/studio/conversations/models.py、tests/studio/test_conversation_models.py。
- 实施：冻结上下文、消息状态、回答任务、proposal采用状态/基准。
- 验收：不同项目/步骤/片段历史隔离；消息不靠当前UI选中解释；重复command不会两条消息。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。

## DB-10 · 媒体、剪辑、发布表

- 依赖：ROOT-03、DB-04、DB-08。白名单：backend/studio/edits/models.py、backend/studio/media/models.py、backend/studio/publishing/models.py、tests/studio/test_edit_models.py。
- 实施：MediaArtifact、EditDraft/Instance、ConfirmedEdit、Release，毫秒时间、源版本及不可变manifest。
- 验收：重复clip使用有独立instance；发布固定确认版本；文件摘要/规格与时间轴可追溯。
- 产品规则：R04, R06, R07, R08, R09, R11，见[产品规范](../../product/product-spec.md)。
