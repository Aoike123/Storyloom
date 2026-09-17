# 演示数据：把一部做完的漫剧搬进另一个部署

日期：2026-09-16。状态：工具已实现（`scripts/demo-data.py`），已用当前工作区的《蓝血》导出并在一套空部署里验证导入。

公网演示需要在**不花任何模型费用**的前提下就有一部可看、可进工作台的成片，而且每次 `reset-production.sh` 之后都要能再装回来。这个工具把某个部署里已经做完的一部作品切出来，连同它实际引用到的媒体文件打成一份数据包，再导进另一个部署。

## 为什么不能直接复制数据库

- 制作过程里混着失败、废弃和历史尝试。演示要展示走通的那条路，不是调试现场。
- 媒体文件按路径引用，缺一个文件，读者目录的卡片就会静默消失（`catalog.playable` 会跳过条目）。
- 工作室只列**属于当前账号**的作品：公网模式下无主作品既不可见也不可认领，所以导入必须把归属一并分配。

## 它带走什么

记录：故事原文、人物/服装/场景素材与快照、镜头修订、导演项目、视觉规范、前期设定与确认门、制作轮次、片段、片段存储记录、选取记录、发布版本、作者作品。

任务：成品链路里 `completed` 的节点与子任务（`author_flow`、`author_storyboard`、`author_render`、`director`、`creative_watch`、`image`、`video`、`art_design`、`author_styles`），以及它们的进度文本与 Skill 调用记录。**失败、废弃、取消的尝试，用量账目、审计记录和失败报告都不导出。**

媒体：只复制上述记录和任务里出现过的 `/media/...` 文件。工作区里的《蓝血》是 65 个文件、113.9 MB。

## 三步走

### 1. 在已经做完作品的那个部署里导出

```bash
python scripts/demo-data.py export --out ../demo-data
```

只有一个导演项目时不用指定；有多个就加 `--director <id>`。`--owner` 可以在导出时就把作品写给某个账号，但更推荐留到导入时再分配。

导出会校验每个被引用的媒体文件都在，缺一个就直接报错退出——宁可不打包，也不生成一个读者打不开的演示包。

### 2. 在目标部署里导入

```bash
python scripts/demo-data.py accounts                      # 这个部署见过哪些知乎账号
python scripts/demo-data.py import ../demo-data           # 导入，并分配给每个已知账号
```

导入是幂等的：记录已存在就跳过，媒体只在大小不同时复制，重复执行不会改动已有内容。它只新增，不删除任何东西，所以可以在一台已经有数据的部署上安全运行。首次导入会顺带建表，空数据库也能直接用。

### 3. 账号是后创建的，就单独分配

导入时还没有任何账号登录过，作品会处于无主状态（公网模式下工作室看不到它，读者目录仍能看到发布版本）。等真实账号登录过一次之后：

```bash
python scripts/demo-data.py assign ../demo-data
```

`assign` 只补归属，不重抄媒体。以后每多一个需要看到演示的账号，再跑一次即可。

指定单个账号、并给它署名：

```bash
python scripts/demo-data.py assign ../demo-data --owner-uid 969570047710216200 \
  --creator-name 桃花先生 --creator-avatar https://picx.zhimg.com/xxx.jpg
```

## 归属与署名

每个目标账号会拿到**自己的作品记录**（id 由作品号与账号按 `work_id_for` 算出，与应用自己创建时一致），指向同一部成片。这是工作室按账号过滤作品的必然结果：一份记录只能属于一个账号。

一份发布版本只能有一个署名。多个账号共享演示时，工具保留原有署名并提示；只有一个目标账号时，自动用该账号的知乎昵称和头像署名，也可以用 `--creator-name` / `--creator-avatar` 显式指定。

## 已知边界

- 共享同一部成片意味着：某个账号在演示作品上点「继续下一个情节」，会真的调用付费模型，并改动所有人共用的那份成片记录。演示账号最好只用于观看和展示。
- 媒体仍按 `/media` 前缀公开提供，链接可分享。演示包里的图片与视频会出现在目标部署的 `data/media` 下。
- 工具不迁移 `usage`、`audit`、`failure_report`，所以导入后的用量统计从零开始，这是有意的。
- 导出用的数据库可以是运行中的实例：工具只读记录，不再修改源部署。

## 在生产 Docker 部署上导入

生产的数据在 PostgreSQL 与 `story_data` 命名卷里，宿主机上的 Python 环境读不到它们，所以要
在 api 容器里运行这个工具。API 镜像只包含 `backend/`，脚本本身要从仓库目录挂进去。

**`scp` 这一步在本地执行，不是服务器上。** 它把文件从「放仓库的这台机器」推到服务器；在服务
器上运行只会得到 `-bash: user: No such file or directory`，因为 `<user>@<server>` 是占位符，
要替换成实际的登录用户与地址。

```bash
# ① 本地：把演示包压成单个文件，比逐文件传输更省事
tar -czf artifacts/demo-data.tar.gz -C artifacts demo-data

# ② 本地：传到服务器的部署目录（bundle 不在版本库里，必须单独传）
scp artifacts/demo-data.tar.gz root@<服务器地址>:/opt/storyloom/

# ③ 服务器：解出 /opt/storyloom/demo-data
cd /opt/storyloom && tar -xzf demo-data.tar.gz

# ④ 服务器：在仓库目录下运行，数据与媒体都写进容器使用的那份卷
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps -T \
  -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/demo-data:/app/demo-data:ro" \
  --entrypoint python api scripts/demo-data.py import /app/demo-data

# ⑤ 服务器：演示账号登录过一次之后，交给它
docker compose --env-file .env.production -f compose.production.yaml run --rm --no-deps -T \
  -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/demo-data:/app/demo-data:ro" \
  --entrypoint python api scripts/demo-data.py assign /app/demo-data
```

不要用 `--user root` 覆盖镜像里的服务账号：命名卷归 `storyloom`（uid 10001）所有，用别的身份
写入会留下容器进程读不了的文件。上面这些命令在本文写作时的 Windows 开发机上没有 Docker
可用于实测，逻辑与媒体路径已在本机以空部署完整验证过。

## 验证方式

`tests/test_demo_data.py` 覆盖：演示包只带成片链路（失败与审计被排除）、导入空部署后账号在工作室看到作品且读者目录可播、重复导入不改变数据、账号后创建时 `assign` 生效并署名、没有账号时给出明确提示、`--owner-uid` 只作用于指定账号。
