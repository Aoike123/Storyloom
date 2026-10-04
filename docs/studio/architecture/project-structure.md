# 前后端项目结构与模块边界

版本：2.0 · 2026-10-04。性质：**本轮重构的目标目录与依赖规则**。当前代码尚未迁入；实现必须按[原子计划](../execution/implementation-plan.md)验收后切换，不能只搬旧文件改名字。

产品行为见[产品规范](../product/product-spec.md)，模型/命令规则见[领域契约](domain-contracts.md)。ROOT-02 冻结具体 schema/API/导出接口；此文决定文件放在哪里、谁负责什么。

## 1. 仓库顶层

```text
Storyloom/
├── web/                      Next.js 前端
├── backend/                  FastAPI、领域服务和新任务执行器
├── tests/studio/             后端新业务与集成验证
├── tests/fixtures/studio/    前后端共享边界数据与本地媒体
├── scripts/                  启动、开发和依赖边界检查
├── docs/studio/              唯一当前设计/执行入口
├── data/                    运行数据与媒体，不能作为测试fixture
├── requirements.txt
└── README.md
```

后端用现有Python包结构，前端保留已有package/lockfile；本轮不为排目录引入额外monorepo工具、重新选框架或升级无关依赖。运行配置与生成文件分开，密钥不进入代码、fixture或文档。

## 2. 前端：路由薄，功能按领域组织

```text
web/
├── app/
│   ├── page.tsx                              项目入口装配
│   ├── studio/[projectId]/page.tsx            唯一工作台路由
│   ├── projects/[projectId]/page.tsx          公开项目详情
│   ├── watch/[releaseId]/page.tsx             发布观看
│   └── layout.tsx                            全站主题/账号外壳
├── features/
│   ├── projects/                             入口、搜索、我的项目与详情
│   ├── viewer/                               真实发布播放
│   └── studio/
│       ├── StudioApp.tsx                     工作台装配与领域接口注入
│       ├── shell/                            顶栏、布局、选择、属性、视图恢复
│       ├── source/                           连续阅读、片段、划选与来源变更
│       ├── canvas/                           受控画布、几何和统一来源连线
│       ├── script/                           动作/对白编辑与采用
│       ├── storyboard/                       场、镜头、输入与正式顺序
│       ├── assets/                           资产规格、版本、上传与审核
│       ├── film/                             左侧资源、实例、时间轴与媒体预览
│       ├── jobs/                             生成命令、队列与事件同步
│       └── conversation/                     步骤消息、历史和提案采用
├── shared/
│   ├── api/                                  HTTP/认证/错误协议，无业务状态
│   ├── auth/                                 账号会话与登录意图
│   ├── ui/                                   无领域规则的基础控件
│   ├── theme/                                语义token、主题与偏好
│   └── media/                                通用文件/播放能力接口
└── tests/studio/                             前端范围、状态与命令验证
```

功能模块内按需设 `components/`、`api.ts`、`types.ts`、状态文件及样式；不预建空层级。业务组件留在所属领域，只有被多个领域实际复用且无领域含义的能力进入shared。不得把所有节点、表单和API堆进一个全局components/services目录。

- `app/*`只解析路由、鉴权和装配，不包含选区算法、业务reducer或生成协议。
- `StudioApp`组合来源、当前工作区、属性、任务与对话；跨模块通过冻结的ObjectRef/查询/命令接口交流，不读兄弟组件内部状态。
- `shell`维护查看上下文；业务确认、任务、剪辑等持久事实由各领域API负责。画布位置与正式顺序分开保存。
- 前端域模块可以依赖shared；shared不能反向依赖features。工作台不得import旧author组件或调用旧业务API。
- `projects`与`viewer`消费新公开/个人API，不从工作台DOM或内部reducer取数据。

## 3. 后端：领域模块拥有模型、服务与HTTP入口

```text
backend/
├── app.py                                    仅启动、通用中间件与路由装配
├── core/                                     数据库连接/Base、配置、鉴权、错误
├── infrastructure/
│   ├── providers/                            无业务状态的供应商协议客户端
│   └── media/                                经核对的纯文件/probe/导出工具
└── studio/
    ├── router.py                             新 /api/studio 路由聚合
    ├── contracts/                            ObjectRef与领域间端口/事件
    ├── projects/                             项目属主、元信息与列表
    ├── sources/                              原文、片段、范围、变更、制作范围
    ├── scripts/                              剧本对象、草稿与采用
    ├── storyboard/                           场、镜头、输入、视频结果
    ├── assets/                               人物/服装/场景、规格及版本
    ├── relations/                            出处、出现与准确生成绑定
    ├── reviews/                              版本审核与依赖影响策略
    ├── jobs/                                 Job/Attempt/Event、lease与调度
    │   ├── runner.py
    │   ├── worker_main.py                    新执行入口
    │   └── handlers/                         受控领域处理器
    ├── conversations/                        会话、上下文读取、提案
    ├── edits/                                草稿、实例、确认manifest
    ├── media/                                媒体事实、私有存储与访问控制
    └── publishing/                           同版导出引用、发布与公开查询
```

领域模块通常包含 `models.py`、`schemas.py`、`service.py`、`api.py`，查询复杂再拆queries；sources中的片段/变更及jobs处理器按任务明确拆文件。字段和外键在所属领域模型中定义，不回填万能Record.data，不让router成为业务服务。

| 层 | 允许职责 | 不允许职责 |
| --- | --- | --- |
| API | 身份/请求解析、调用命令、序列化错误 | 直接改兄弟领域表或在GET创建业务 |
| 领域服务 | 归属/范围/版本校验、事务、命令幂等、影响 | 发起未登记供应商请求、从UI标签推断任务 |
| 模型 | 权威表、约束、版本身份 | 请求网络、计算画布、import旧管线 |
| jobs编排 | 冻结输入、lease、attempt、事件与处理器调度 | 复制所有领域业务、靠当前页面补任务目标 |
| infrastructure | 纯协议/文件能力，通过显式参数工作 | 写旧Task/Record、确认资产、改变采用状态 |

领域可依赖core/contracts；跨领域写入必须经过服务端口。涉及多个领域的采用/生产/发布由明确编排服务执行，ROOT-02确定事务边界与注册位置；不能形成互相import的service环。外键不是导入整个邻域服务的理由。

旧workflow文件、专业节点绑定、阶段门槛、prompt payload及旧管线测试不约束新模块。领域依赖/审核/调度以当前产品规则和新冻结契约为准；重构不能变成把旧workflow搬到studio目录。

`StudioJob`是新任务唯一状态权威。worker只使用新处理器及显式注入的adapter。供应商返回通过处理器登记实际媒体/领域revision，随后等待准确版本的审核/采用。

## 4. 测试、装配与退役

用户已明确授权删除重构项目的旧版代码。旧产品文件按已审定清单执行退役，不再另设用户删除许可步骤；依赖和复验要求用于保证重构交付完整。

测试与业务目录按领域对应；共享文本/emoji/换行/range fixture只有一份。后端测试固定隔离数据库与媒体目录，前端DOM/状态测试不注入普通项目的样例，媒体一致性使用本地时间码和声音样片。

BASE建立新命名空间；ROOT-02定基础设施提取白名单；各领域任务创建新实现；RETIRE检查依赖并删除旧产品实现。原 `backend/db.py/auth.py/providers.py/video_storage.py` 不能整文件盲搬：只提取经核对的连接/Base、账号、纯协议/纯文件能力，旧业务写入必须解除。

旧 `/author`、`/author/workbench`、旧业务API、阶段管线、旧watch入口与专用样式在门槛通过后删除，不保留双工作台或兼容跳转。真实数据/媒体处置不属于此目录重构。删除名单和先后关系见[依赖盘点](../evidence/legacy-dependency-inventory.md)。

每张卡指定具体新文件；执行器不能把旧组件移动进新目录冒充重构。目录与依赖检查、构建、真实页面和正确运行基线均通过后，才能报告“前后端结构整理完成”。本轮文档只确定目标结构。
