# 原文与片段卡

版本：2.1 · 当前任务定义。顺序与派发规则见[实施计划](../implementation-plan.md)，实际状态只见[账本](../acceptance-ledger.json)。

路径以[项目结构](../../architecture/project-structure.md)为准。任务族父卡只汇总，不派发；执行器收到下面一张单行为子卡及已冻结接口。主模型派发时收窄白名单至至多3个实现文件加直接验证，并填入准确命令/操作与负向情形。

## SOURCE-01 · 新项目与来源读写API

- 依赖：DB-01、BASE-02。白名单：backend/studio/projects/service.py、backend/studio/projects/api.py、backend/studio/sources/service.py、backend/studio/sources/api.py、tests/studio/test_projects.py；逐命令子卡收窄。
- 实施：新建/读取/个人列表、导入原文和来源读取；严格属主；导入返回真实source身份。
- 验收：新建不要求先有原文，空态准确；raw内容未trim，来源可重读；未登录/跨属主不能读私有内容；无旧API调用。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### SOURCE-01-a · 新建空项目

- 唯一目标：创建只有项目元信息的私有项目，不隐式导入。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-01-b · 读取单项目

- 唯一目标：准确读取属主项目；未登录/跨属主拒绝。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-01-c · 个人项目列表

- 唯一目标：仅返回当前账号项目，空列表与读失败分开。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-01-d · 导入不可变原文

- 唯一目标：保存raw与canonical并返回真实来源身份，不生成片段。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-01-e · 读取来源版本

- 唯一目标：读取指定来源正文/摘要，跨项目引用拒绝。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## SOURCE-02 · 统一字符与范围helper

- 依赖：ROOT-02。白名单：backend/studio/sources/ranges.py、web/features/studio/source/ranges.ts、tests/fixtures/studio/ranges.json、tests/studio/test_ranges.py、web/tests/studio/ranges.test.cjs。
- 实施：LF规范、UTF-16边界/切片、连续非空与相交；前后端共用fixture。
- 验收：契约所有emoji/CRLF/空白/相接/包含案例一致；不直接用Python offset或DOM行号保存。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-03 · 原子保存候选

- 依赖：SOURCE-01～02、DB-02。白名单：backend/studio/sources/fragment_service.py、backend/studio/sources/fragments_api.py、tests/studio/test_fragments.py。
- 实施：创建candidate、稳定ID、集合CAS与command_id；候选和确认共同防重叠。
- 验收：两个并发请求最多一个成功，另一409给冲突目标；重发同command无重复；刷新能见候选，未自动确认/生成。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

### SOURCE-03-a · 片段列表与详情读取

- 依赖：SOURCE-03。白名单：backend/studio/sources/fragment_service.py、backend/studio/sources/fragments_api.py、tests/studio/test_fragments.py。
- 唯一目标：F2 片段列表 + F3 片段详情（附录 02 §2）——计划缺口补登：F2/F3 行原无所属卡，SOURCE-12 前端视图依赖之（2026-10-06 主模型补录）。
- 验证：列表只含 active 版本片段、有效状态与游标分页准确；详情对非 active 片段派生 pending_review 且 revision_is_active 真实；读取失败/空列表/不存在三者分开；未登录/跨属主拒绝。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-04 · 明确确认片段

- 依赖：SOURCE-03、DB-07。白名单：backend/studio/sources/fragment_service.py、backend/studio/sources/fragments_api.py、tests/studio/test_fragments.py。
- 实施：确认当前revision并保存ReviewDecision，再校验source/集合版本。
- 验收：过期确认拒绝，重复确认幂等；确认不要求全文覆盖，也不表示已有剧本/视频。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-05 · 片段改名与退役命令

- 依赖：SOURCE-03。白名单：backend/studio/sources/fragment_service.py、backend/studio/sources/fragments_api.py、tests/studio/test_fragments.py。
- 实施：主模型按“改名”“退役”分别派两次，保持ID和revision/历史；退役不删除产物。
- 验收：改名不换ID；退役解除当前有效范围占用，出处仍可读；不能通过删除记录回避历史引用。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### SOURCE-05-a · 片段改名

- 唯一目标：只改名称，身份及来源依据不变。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-05-b · 片段退役

- 唯一目标：只退役有效范围，保留历史产物依据。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

## SOURCE-06 · 边界调整命令

- 依赖：SOURCE-04～05、DB-07。白名单：backend/studio/sources/fragment_service.py、backend/studio/sources/fragments_api.py、backend/studio/reviews/impact.py、tests/studio/test_fragment_changes.py。
- 实施：预览影响后更新范围revision，同一规则检查重叠和基准。
- 验收：已有下游正确标待复核；旧视频输入仍指旧范围；CAS失败无半次改变。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-07 · 片段拆分命令

- 依赖：SOURCE-06。白名单：backend/studio/sources/fragment_service.py、backend/studio/sources/fragments_api.py、tests/studio/test_fragment_changes.py。
- 实施：两新实体/predecessor、旧实体退役、集合一次事务改变。
- 验收：范围合法且相接时可拆；失败回滚整次；旧对象来源不偷偷迁给一半。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-08 · 片段合并命令

- 依赖：SOURCE-06。白名单：backend/studio/sources/fragment_service.py、backend/studio/sources/fragments_api.py、tests/studio/test_fragment_changes.py。
- 实施：仅合法连续范围合成新实体，记录继承及影响；非连续不假拼接。
- 验收：冲突/间隙/过期明确拒绝；新ID及历史关系可读；不自动重生成。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-09 · 正文变更影响预览

- 依赖：SOURCE-06、DB-06～07。白名单：backend/studio/sources/changes.py、backend/studio/sources/changes_api.py、tests/studio/test_source_changes.py。
- 实施：返回确定影响、来源映射待复核、历史保留及运行中冻结输入；preview零写业务。
- 验收：插字/删选区/移段/重复字串不会假称恢复；未提交时旧正文/对象完全可用。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-10 · 显式应用新正文

- 依赖：SOURCE-09。白名单：backend/studio/sources/changes.py、backend/studio/sources/changes_api.py、tests/studio/test_source_changes.py。
- 实施：校验preview基准，创建新不可变source，受影响关系待复核；新任务用新来源。
- 验收：过期preview409可重算；旧任务/视频/剪辑/发布保持实际依据；不把旧范围直接套到新正文。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-11 · 完整制作范围提交

- 依赖：SOURCE-04、SOURCE-10。白名单：backend/studio/sources/scopes.py、backend/studio/sources/scopes_api.py、tests/studio/test_scopes.py。
- 实施：独立检查指定制作范围的顺序、首尾覆盖、裁减/范围外记录，固定采用的范围revision。
- 验收：候选允许正文未分配；完整提交缺口有定位；长作品按范围/执行窗口安排，没有继承旧片段数上限。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-12 · 连续原文与动态片段视图

- 依赖：SOURCE-03～04、BASE-02。白名单：web/features/studio/source/components/SourceReader.tsx、web/features/studio/source/components/FragmentList.tsx、web/features/studio/source/api.ts。
- 实施：服务端canonical文本连续阅读，区域及动态列表共用真实ID；读取/空态/错误分开。
- 验收：无卡片/标题/状态插入正文，无自动样例；列表可点击定位、确认状态真实；读失败不伪装为空故事。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-13 · 原生选区建立候选

- 依赖：SOURCE-12、SOURCE-02。白名单：web/features/studio/source/components/SourceReader.tsx、web/features/studio/source/components/SelectionToolbar.tsx、web/tests/studio/selection.test.cjs。
- 实施：正反/跨行DOM选区准确映射，pending/取消/新建分开；创建命令成功才用服务端ID，冲突可定位。
- 验收：emoji和换行切片一致；Esc取消不删已保存候选；重叠不静默裁切；新区域立即可见且不自动切剧本。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。

## SOURCE-14 · 来源命令与变更复核工具

- 依赖：SOURCE-05～11、SOURCE-13。白名单：web/features/studio/source/components/FragmentActions.tsx、web/features/studio/source/components/SourceChangeReview.tsx、web/features/studio/source/api.ts。
- 实施：每次只派一种确认/改名/退役/边界/拆合/正文复核UI命令，接已确定API；失败保留内容。
- 验收：保存/确认/应用为可辨认动作，刷新结果一致；过期/断网可恢复；更换正文成功前不退出表单丢错误。
- 产品规则：R03, R04, R11，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### SOURCE-14-a · 确认片段UI

- 唯一目标：接明确确认命令，选择不确认。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-14-b · 片段改名UI

- 唯一目标：接改名命令，失败保留输入。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-14-c · 片段退役UI

- 唯一目标：展示影响后明确退役，不删除产物。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-14-d · 边界调整UI

- 唯一目标：预览新范围后应用，冲突可定位。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-14-e · 片段拆分UI

- 唯一目标：明确拆分点与继承，原子提交。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-14-f · 片段合并UI

- 唯一目标：只合并合法范围，旧引用不自动归新对象。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-14-g · 正文变更复核UI

- 唯一目标：预览/明确应用新版本，失败不退出表单。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### SOURCE-14-h · 完整制作范围UI

- 唯一目标：明确覆盖/裁减/范围外清单，独立于候选创建。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。
