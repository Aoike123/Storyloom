# 步骤对话卡

版本：2.1 · 当前任务定义。顺序与派发规则见[实施计划](../implementation-plan.md)，实际状态只见[账本](../acceptance-ledger.json)。

路径以[项目结构](../../architecture/project-structure.md)为准。任务族父卡只汇总，不派发；执行器收到下面一张单行为子卡及已冻结接口。主模型派发时收窄白名单至至多3个实现文件加直接验证，并填入准确命令/操作与负向情形。

## CHAT-01 · 会话和消息API

- 依赖：DB-09、ROOT-04、BASE-02。白名单：backend/studio/conversations/service.py、backend/studio/conversations/api.py、tests/studio/test_conversations.py。
- 实施：冻结context、历史/消息/状态和幂等，属主/版本范围校验。
- 验收：五步骤/片段可恢复，不串账号/项目；发送时记录对象revision，不在回答时猜当前选择。
- 产品规则：R10，见[产品规范](../../product/product-spec.md)。

## CHAT-02 · 真实问答与提案处理器

- 依赖：CHAT-01、JOB-01～03、ROOT-04。白名单：backend/studio/jobs/handlers/conversation.py、backend/studio/conversations/context_reader.py、tests/studio/test_conversation_jobs.py。
- 实施：仅查询冻结范围的实际数据，走新adapter，回答/出处/结构化proposal；错误/超时为真状态。
- 验收：不是预制回复；假供应商验证上下文；handler不会确认、生成、采用、发布或调用任意函数。
- 产品规则：R10，见[产品规范](../../product/product-spec.md)。

## CHAT-03 · 五步骤底部发送与历史

- 依赖：CHAT-02、VIEW-01、EDIT-06。白名单：web/features/studio/conversation/components/StepConversation.tsx、web/features/studio/conversation/conversation-session.ts、web/features/studio/conversation/api.ts。
- 实施：固定界面层输入、历史/未发送文本恢复，异步回答归原会话；不强制agent/model名字。
- 验收：film同等可问；缩放不缩输入；切去别片段慢回答不抢焦点；失败可重试，发送按钮实际可用。
- 产品规则：R10，见[产品规范](../../product/product-spec.md)。

## CHAT-04 · 明确采用proposal

- 依赖：CHAT-03、SOURCE-04、SCRIPT-01、ROOT-04。白名单：backend/studio/conversations/proposals.py、backend/studio/conversations/api.py、web/features/studio/conversation/components/ProposalReview.tsx、web/features/studio/conversation/api.ts、tests/studio/test_proposals.py；按命令/UI子卡收窄。
- 实施：按白名单领域命令显示目标/影响，明确确认后校验版本/幂等采用；不是模型直接写库。
- 验收：查看/聊天零业务采用；过期重新预览；重复proposal不重复操作；失败无半次变更。
- 产品规则：R10，见[产品规范](../../product/product-spec.md)。
- 边界：任务族父卡不可派发，以下单操作子卡全部验收后才接受父卡。子卡继承父卡验收要求，仅实现其唯一目标。

### CHAT-04-a · proposal采用命令

- 唯一目标：校验基准/白名单/影响后明确应用，重复幂等。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。

### CHAT-04-b · proposal采用UI

- 唯一目标：展示差异与明确采用，回答不直接改业务。
- 范围：继承父卡允许区域，主模型按此操作收窄具体文件；其余兄弟操作不在本次范围。
- 验证：父卡中与该操作直接相关的成功/失败/恢复情形；主模型独立复验，未冻结接口时不派发。
