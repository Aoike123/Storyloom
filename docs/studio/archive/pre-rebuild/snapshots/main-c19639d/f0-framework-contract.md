> **历史记录，已退出当前设计与实施依据。** 文中版本、完成状态、旧入口及迁移方案仅描述当时背景；当前规则从[文档入口](../../../../README.md)读取。

# 工作台框架接口契约（F0）

版本：1.4 · 2026-10-03 · 状态：**F0–F4 框架已完成**；本文保留当时框架契约
上位文档：[框架开发计划](implementation-plan.md)（F0） · 空间方向：[画布优先设计方向](canvas-design-direction.md) · 领域约束：[设计规范](design-spec.md)

> **最新设计与框架记录的边界**：本轮已确认左侧连续全文／荧光片段、动态跳转、区域实际边缘统一出线、所选对象属性、底部步骤对话及全尺寸成片编辑布局，详见[当前方向](canvas-design-direction.md#7-故事总控连续原文与荧光片段)。本文的四个 Workspace、无左侧栏和只读成片占位是已完成 F0–F4 的历史实现契约，不再限制下一阶段设计，也不因文档更新自动迁移；后续工作见[工程安排](implementation-plan.md#7-本轮确认设计与后续工程安排)。以下接口定义保留，不将设计稿状态伪装为新增生产字段。

> 本文是 **F0 的交付物**：一份可直接开发的**框架接口定义**，只锁定入口、组件边界、展示模型、视图状态、画布视野、主题边界与旧页兼容范围。**不包含**任何业务操作契约（预处理、分镜编辑、素材分配、素材生成、审核、生产调度、剪辑、发布、导出的操作规则均待 F4 后的 D 任务讨论）。
>
> **v1.1 修订**：① 画布由"自研轻量实现"改为**引用现成节点画布库 React Flow（`@xyflow/react` v12）**，于 F2 引入；② 按只读勘察校准：主题为**作用域类换肤**（无运行时切换引擎）；③ 按 `backend/authors.py` 实测校准 §7 的真实数据字段。

> **v1.3 修订（F4 验收收敛）**：① 成片（release）落地为**独立工作区容器占位**（只读片段列表，不接剪辑时间轴内核），与 story/production/assets 的无限画布区分；② 画布移除 React Flow **自带 Controls 控件面板**，改用工作台自有的「适配视野」按钮（平移 / 滚轮缩放 / 适配仍保留）；③ §5 默认落地工作区明确为「制作 · 项目主体」。

## 1. 入口与路由

- 新入口 `web/app/author/workbench/page.tsx`，Next.js 15 **app-router client component**（`'use client'`），与现有 `web/app/author/page.tsx` **并存**。
- 保留 `?work=<projectId>` 作为项目标识。**不处理** `?story=<sourceId>`：新故事导入继续走现有 `/author` 的 `stories/{story}/open` 路径；工作台只承接已存在的 `work`。
- **不替换、不修改**现有 `/author` 页面、`projectProgress.ts`、`useProjectProgress.ts`、`author.css` 及任何后端流程；现有生产路径与任务恢复保持不变。
- 后端数据仍经 Next `next.config.ts` 的 rewrite 代理：`/api/:path* → $STORYLOOM_BACKEND_URL（默认 http://127.0.0.1:8000）/api/:path*`，`/media/:path* → …/media/:path*`。工作台复用同一代理，**不新增** Next route handler。
- 现有 `/author` 目录**无子目录**，新建 `workbench/` 干净落地；现有 15 个文件互不干扰。

## 2. 工作区模型（项目的四个工作区视图）

工作台以**项目（project）为核心单位**：入口绑定一个项目，故事／制作／素材／成片是该项目的四个视图；**故事是项目的来源素材**，不是组织核心或归属根。

```ts
type Workspace = "story" | "production" | "assets" | "release";
// 显示名：故事 / 制作 / 素材 / 成片
```

- **工作区 ≠ 后端生产 `stage`**。`work.stage` / `work.display_stage`（`style|segments_review|preparing|assets_review|storyboarding|rendering|episode_review|film_review|published`）是**服务端权威的生产进度**，现有回看用 URL `?step=` 承载；这些**都不**用来切换用户当前所在工作区。
- 现有页面**没有顶部标签栏**、**没有路由子页**；四个工作区是**全新的前端视图态**（`activeWorkspace`），与 `stage`、`?step=` 严格分离，只用它装配"看哪个工作区的内容"。
- 只有用户点击顶部标签才改变 `activeWorkspace`；后台阶段更新、查看对象、引用跳转**都不改变** `activeWorkspace`（无自动跳标签）。
- `story/production/assets` 采用无限画布；`release`（成片）本期为**独立工作区容器占位**（不接剪辑时间轴内核）。

## 3. 组件边界

| 模块     | 文件                                | 单一职责                                                         | 关键 props / 暴露                                                      |
| -------- | ----------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------- |
| 入口     | `page.tsx`                          | 读取 `?work`，装配 Shell，持有 `WorkbenchViewState`              | `<WorkbenchShell view data />`                                         |
| 外壳     | `WorkbenchShell.tsx`                | 顶栏 + 四标签 + 主内容区 + 右侧区域布局与主题根                  | 接收 view 与 adapter 数据；装配当前 Workspace 组件与 ContextPanel      |
| 标签     | `WorkspaceTabs.tsx`                 | 用户主动切换工作区                                               | `active`, `onSelect(w: Workspace)`；不承载阶段语义                     |
| 画布视野 | `CanvasViewport.tsx`                | **封装 React Flow** 的平移/缩放/适配/自由摆卡/按场排列           | 接收 `viewport` 与节点渲染器；`onViewportChange`（F2 实现，F1 先占位） |
| 节点     | `SceneFrame.tsx`、`PreviewNode.tsx` | React Flow **自定义节点**：场景分组框 / 展示节点，纯展示不写业务 | 渲染 `DisplayNode`；选中态回调                                         |
| 右侧面板 | `ContextPanel.tsx`                  | 开合 + 选中摘要 + 内容插槽（属性/引用/任务/定位的**占位**）      | `open`, `selectedId`, `onClose`；插槽接口                              |
| 视图状态 | `workspace-state.ts`                | `WorkbenchViewState` 最小契约与 reducer                          | 见 §5                                                                  |
| 只读适配 | `project-adapter.ts`                | 现有 `work` 对象 → 展示模型的只读映射 + 空态                     | 见 §4                                                                  |
| 样式     | `workbench.css`                     | 工作台专属布局，复用全局 token/控件/动效                         | 规则限定在工作台根节点 `.workbench`                                    |

规则：

- 各工作区是**独立组件**，避免一个巨型条件渲染页面；`WorkbenchShell` 只按 `activeWorkspace` 装配对应工作区组件与右侧面板。
- 组件之间只通过 `WorkbenchViewState` 与 adapter 展示数据通信；**不得**在组件内直接读 `work` 业务字段做流程判断。
- 右侧面板本期**只放只读摘要/占位**，不移植样式稿里的任何编辑表单、生成表单或版本浏览器。
- **画布引擎 = React Flow（`@xyflow/react` v12）**：本框架只用其**视野（平移/缩放/适配）+ 节点视图（自由摆卡/整齐排列）+ 自定义节点渲染**；**不启用/不暴露**连线（edges）、多选、批量、子流程等能力（留待 D02/D05）。依赖在 **F2** 用 `pnpm add @xyflow/react` 引入；F1 的画布区先为占位容器。

## 4. 展示模型与只读适配（`project-adapter.ts`）

框架只定义**轻量展示字段**，不复刻 [设计规范 §11](design-spec.md#11-拟新增的数据契约) 的完整业务契约（那是数据层，留待后续）：

```ts
type NodeKind = "source" | "script" | "scene" | "shot" | "asset" | "clip";
interface DisplayNode {
  id: string; // 稳定对象 id（取自 work 数据，或示例标记）
  kind: NodeKind;
  title: string; // 展示标题；缺失显示空态，不伪造
  group?: string; // 场景/分组 id（用于按场整齐排列）
  isExample?: boolean; // 隔离示例数据标记，见下
}
interface WorkspaceContent {
  workspace: Workspace;
  nodes: DisplayNode[];
  emptyReasons: Partial<Record<NodeKind, string>>; // 缺失内容的明确空态
}
```

适配规则（`mapProject(work): Record<Workspace, WorkspaceContent>`，只读纯函数、不写库）：

- 数据源为现有 `GET /api/author/projects/{id}` 返回的 `work`（结构见 §7）。
- **真实数据映射**：`source`←`work.source`/`title`；`shot`←`work.creative.production.shots[].shot`；`asset`←`work.creative.items[]`（`role: character|costume|scene`）及其 `asset` 记录；`clip`←`shots[].clip`（`media`/`duration`）。
- **空态优先**：现有数据没有的"剧本/场次/篇章"结构 → `script`/`scene` 走明确空态（"该故事尚未编排场次/剧本"），**不创建临时业务记录**补齐。
- **示例隔离**：多场景/多镜头的展示用**隔离示例数据**，节点必须带 `isExample: true`，UI 要有可辨识标记；示例**绝不写入真实项目**，也不用它暗示后端已支持新故事结构。
- **映射到 React Flow**（F2）：`DisplayNode.id→node.id`、`kind→node.type`（对应自定义节点组件）、坐标→`node.position`（自由摆卡取 `layout`，整齐排列由 `group` 计算对齐）、`{title,group,isExample}→node.data`。**不使用** React Flow 的 edges。
- 展示位置（坐标）不属于 `DisplayNode` 内容，而属于 §5 的每工作区视野/排布，二者分离。

## 5. 视图状态契约（`workspace-state.ts`）

框架（前端）拥有的状态，与业务状态严格分离：

```ts
interface Viewport {
  x: number;
  y: number;
  scale: number;
}
interface WorkspaceView {
  viewport: Viewport; // 平移/缩放（F2 与 React Flow 视野双向同步）
  selectedId: string | null; // 当前查看对象，至多一个
  panelOpen: boolean; // 右侧面板开合
  layout?: Record<string, { x: number; y: number }>; // 自由摆卡坐标（视图偏好）
}
interface WorkbenchViewState {
  projectId: string;
  activeWorkspace: Workspace;
  byWorkspace: Record<Workspace, WorkspaceView>;
}
```

- **按 项目 × 工作区 隔离**视野、选择、面板与摆卡；切换工作区恢复该工作区自己的视图，互不干扰。
- **切换仅由用户发起**（点标签）；`popstate`/后台更新不改 `activeWorkspace`。
- **会话内保留、刷新回默认**：视图状态在会话内保留（前端内存）；刷新回到合法默认视图（默认落地工作区「制作 · 项目主体」+ 默认视野）。跨设备/服务端保存不在本期。
- **展示 ≠ 业务**：平移/缩放/自由摆卡/整齐排列**只改展示位置**，不改归属、镜头顺序、素材绑定或确认状态；画布不从坐标推断任何业务含义。
- F2 中 `WorkspaceView.viewport` 与 React Flow 的内部 viewport 通过 `onMoveEnd→写入`、装配时`读入`双向同步，实现"切换工作区/刷新后恢复视野"。

## 6. 画布视野（`CanvasViewport.tsx`）— 引用 React Flow

- **引擎**：React Flow（`@xyflow/react` v12，旧包名 `reactflow`）。F2 用 `pnpm add @xyflow/react` 引入；React 19 peer 兼容性在 F2 安装时实测——如遇 peer 冲突，用 pnpm 的依赖解析（如 `pnpm.overrides` / 接受 peer）处理，**不因此退回自研**。
- **使用范围**（仅视图/节点能力）：视野平移（拖空白）、缩放（滚轮/按钮）、适配（`fitView`）、节点**自由摆卡**（拖拽改 `layout` 坐标）、**按场整齐排列**（按 `group` 计算对齐坐标；生产默认"场景分组 + 横排镜头"，素材独立资源节点）。
- **自定义节点**：`SceneFrame`（场景分组框）与 `PreviewNode`（镜头/资源/成片节点）渲染 `DisplayNode`，展示 `title` 与 `isExample` 标记。
- **不启用/不暴露**：连线绘制与编辑、多选、批量勾选、子流程、React Flow 自带控件面板——这些不在本期框架，留待 D02/D05 讨论后再决定是否开放。
- 画布组件**不从坐标推断**归属或叙事顺序；顺序来自数据的 `group` 与展示排序。

## 7. 现有数据形状（适配输入，来自 `GET /api/author/projects/{id}`）

后端处理器：`backend/authors.py` 的 `workspace(pid)`（`@router.get('/projects/{pid}')`）。字段据实测校准（只列适配会用到的关键项，其余经 `record_dict` 展开）：

```ts
type Work = {
  id: string; // 'work_story_<sha256[:32]>'
  version: number;
  title: string;
  stage: AuthorStage; // 与 display_stage 一致
  display_stage: AuthorStage;
  source_id?: string;
  source_work_id?: string;
  run_id?: string;
  art?: string;
  tone?: string;
  worker_online: boolean;
  source?: {
    // 来源微小说
    title: string;
    work_id: string;
    author_name: string;
    labels: string[];
    content: string;
    source?: string;
    source_url?: string;
    fetched_at?: number;
    completeness?: string;
  };
  task?: Task;
  recommend_task_status?: Task;
  jobs: Task[];
  production_steps: Array<{
    id: "storyboarding" | "rendering";
    name: string;
    hint: string;
    task?: Task;
  }>;
  outputs: Array<{
    id: string;
    kind: "image" | "video";
    title: string;
    media: string;
    production_phase?: string;
  }>;
  episodes: Episode[]; // 分段/集 进度
  storyboard_review?: { approved?: boolean; issues?: string[] };
  creative?: {
    stage?: AuthorStage;
    art?: string;
    tone?: string;
    items?: Array<{
      // 资产（角色/服装/场景）
      role: "character" | "costume" | "scene";
      name: string;
      task_id?: string;
      design?: string;
      character_id?: string;
      character_ref?: string;
      costume_id?: string;
      task?: Task;
      asset?: Asset;
    }>;
    production?: {
      project_id: string;
      version: number;
      approved?: boolean;
      shots?: Array<{
        shot: Shot; // { id, dramatic_action, generation_seconds, edit_seconds, assets, continuity_in, continuity_out, … }
        video_task?: Task;
        clip?: Clip; // { id, version, media, duration }
        storage?: unknown;
      }>;
    };
  };
};
type AuthorStage =
  | "style"
  | "segments_review"
  | "preparing"
  | "assets_review"
  | "storyboarding"
  | "rendering"
  | "episode_review"
  | "film_review"
  | "published";
type Task = {
  id: string;
  kind: string;
  status: TaskStatus;
  production_phase?: string | null;
  production_node?: string | null;
  progress?: number;
  message?: string;
  label?: string;
  created?: number;
  session_id?: string;
  revision?: number;
  mode?: string;
  generation?: unknown;
  provider_error?: unknown;
  skill_calls?: unknown[];
  activity?: unknown;
  preview?: string | null;
  revision_of?: string | null;
  failure_code?: string | null;
};
type TaskStatus =
  | "queued"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "needs_review"
  | "cancelled"
  | "superseded";
//  前三个=BUSY(activeStatuses)；failed/needs_review=PROBLEM(problemStatuses)；其余=终态
type Asset = {
  id: string;
  version: number;
  name: string;
  type: string;
  asset_kind?: string;
  description?: string;
  status?: string;
  media: string;
  layout?: unknown;
};
type Clip = { id: string; version: number; media: string; duration?: number };
type Episode = {/* episode_progress：分段/集 的发布与进度字段 */};
```

> 字段以 `backend/authors.py` 实际返回为准（F0 一手核对 + 只读勘察）。**任何缺失字段一律在 §4 走空态**，不伪造。`Task.status` 取值沿用现有 `ProgressFeedback` 的 `activeStatuses`（`queued/running/waiting`）与 `problemStatuses`（`failed/needs_review`）。

## 8. 主题与样式边界（作用域类，无运行时引擎）

- **现状没有运行时主题切换**：源码无 `prefers-color-scheme`、无 JS 主题 hook/state。主题靠**作用域类**换肤——
  - **亮色（工作台）**：`:root` 默认 token（`globals.css`：`--bg:#f7f7f3; --paper:#fff; --ink:#202d39; --muted:#8a9193; --line:#e6e8e4; --accent:#d57451; …`），现有工作台挂在 `<main class="author-page">`。
  - **暗色（读者首页）**：`.reader-world` 作用域**直接写死颜色**并**重写同名 `--control-*`**，共享原语（`.button`/`.checkbox`）据此自动换肤。
- **框架做法**：工作台新页挂到**新的根类 `.workbench`**，复用 `:root` 亮色 token + `ui-controls.css`/`ui-motion.css` 的共享原语；**不新增**主题引擎、不新增 token、不做运行时切换。设计稿设想的"白日/暗夜/跟随系统"运行时偏好属 **D08**，不在本期。
- 新增 CSS **限定在 `.workbench` 作用域内**，不污染全局；沿用现有控件几何（`--control-height` 46px、`--control-radius` 10px 等）与语义色。
- **类命名**沿用现有**扁平前缀**惯例（**非 BEM**）：工作台新组件用 `workbench-` 前缀（`workbench-topbar`/`workbench-tabs`/`workbench-canvas`/`workbench-panel`…）；状态修饰用独立工具类（`is-current`/`is-selected`…）。
- 动效遵循 `prefers-reduced-motion`（`ui-motion.css` 已有媒体查询 + JS `reducedMotion()`）；面板开合、视野变化用透明度/小幅位移，不循环、不以发光代替选中。

## 9. 旧页兼容范围（不做什么）

- **保留**：现有 `/author` 页面与其 9 阶段流程、`projectProgress.ts`/`useProjectProgress.ts`/`author.css`、后端 `authors.py` 等全部流程、任务恢复、发布。
- **不新增** Next route handler；工作台经现有 rewrite 只读取数。
- **不改**后端数据表、供应商流程、专业节点；不迁移历史项目（映射不完整时继续走现有入口）。
- 工作台**只读**现有项目；原 `/author?work=<id>` 入口与任务恢复路径继续可用。

## 10. 明确排除（非目标）

预处理/精简扩写、剧本编排写入、分镜新增拆合重排、素材分配编辑、素材生成与版本选用、镜头生成与审核、镜头库与时间轴、剪辑内核、发布与导出，以及**跨工作区自动跳转、右侧版本入口、左侧常驻栏**——**均不在本期框架范围**，留待 F4 后的 D 任务。

> 注：React Flow 技术上支持连线编辑、多选、批量勾选等能力，但**本框架不启用/不暴露**它们；这些操作规则留待 D02/D05 讨论后再决定是否开放。

## 11. 当前批次（F0）工作量估计

- **F0 本身**：纯文档契约（本文）+ 对现有代码/`authors.py`/主题的一手核对 + 只读勘察佐证。**无代码改动**，**已完成**（约 1 个开发回合）。
- 面向后续批次的前瞻估计（不含业务，按本契约）：
  - **F1 外壳**（顶栏 + 四标签 + 主内容区 + 右侧面板容器 + 双主题，新目录 `workbench/`；画布区先占位）：约 **1** 回合。
  - **F2 基础画布**（`pnpm add @xyflow/react`；React Flow 视野/节点/自定义节点 + 每工作区视野持久化）：约 **1** 回合（引用成熟库后比自研更轻，主要工作量在展示模型↔节点映射与视野同步）。
  - **F3 只读适配**（`mapProject` 真实映射 + 空态 + 示例隔离，接现有 `work`）：约 **1** 回合。
  - **F4 框架验收**（入口兼容、主题、布局、面板、视图状态走查 + 收敛）：约 **0.5–1** 回合。
  - F1→F4 合计约 **3.5–4** 个开发回合，每批单独提交、单独验收。精确人天在 F1 开工时按实际代码核对再给。
