# 附录 08 · 剪辑内核、媒体与预览/导出（ROOT-03）

版本：2.1 · ROOT-03 冻结 · 2026-10-04。覆盖：剪辑内核选型、EditManifest、支持操作、预览/导出路径、媒体格式与存储、取消/失败/重试、预览-导出一致性门槛。依据：[产品规范 R09](../../product/product-spec.md)、[领域契约 §6](../domain-contracts.md)、[PoC 证据](../../evidence/media-kernel-poc-2026-10-04.md)。

实施位置：纯导出/探测工具 `backend/infrastructure/media/`（从 PoC 脚本提取，主模型核对）；媒体域 `backend/studio/media/`；剪辑域 `backend/studio/edits/`；交付/发布 `backend/studio/publishing/`；前端 `web/features/studio/film/` + `web/shared/media/`。

## 1. 内核选型（冻结）

| 部件 | 选择 | 说明 |
| --- | --- | --- |
| 导出 | **ffmpeg 7.0.2-static**（imageio-ffmpeg 0.6.0 携带，已在 requirements.txt；subprocess 调用，**不链接**进应用） | GPL-2.0-or-later 二进制以独立进程运行，应用代码不受其许可证约束；imageio-ffmpeg Python 包为 MIT。不升级/更换版本除非主模型另立任务 |
| 探测 | `ffmpeg -i` stderr 解析（静态构建无 ffprobe） | 时长/流/尺寸；封装为 `backend/infrastructure/media/probe.py` |
| 预览/监看 | **浏览器原生**：每实例 = 一个源 media element（`<video>/<audio>`），`currentTime=source_in_ms/1000`、播至 `source_out_ms`、`playbackRate=speed`；无客户端解码/转码/WASM | 预览与导出消费**同一 EditManifest**（§2） |
| 参考实现 | `scripts/media_kernel_poc.py`（已验收） | 命令推导 `derive_ffmpeg_cmd` 与验证逻辑是提取基线；该脚本同时是 fixture 生成器与测试 oracle |

## 2. EditManifest（唯一编辑依据，preview 与 export 共用）

```json
{
  "schema": "storyloom.editmanifest/v1",
  "canvas": { "width": 320, "height": 240, "fps": 25 },
  "instances": [
    { "id": "01J8…", "track_id": 0, "timeline_start_ms": 0,
      "media_id": "01J8…", "media_path": "a/b.mp4",
      "source_in_ms": 2000, "source_out_ms": 7000, "speed": 1.0 }
  ],
  "output": { "container": "mp4", "video": "h264", "audio": "aac",
              "width": 320, "height": 240, "fps": 25, "sample_rate": 44100 }
}
```

- 时间为**整数毫秒**（契约 §6）；`instances` 按 `timeline_start_ms` 排序；v1 仅 `track_id=0`（单轨顺序拼接），模型保留 track_id 字段。
- `ConfirmedEdit = {id, edit_id, edit_revision, manifest_digest, output_spec, instances_snapshot}`：`manifest_digest` = sha256(规范化 JSON，键排序、整数毫秒)；预览/导出/发布绑定同一 ConfirmedEdit（R09）。
- 草稿编辑（EditDraft）每次保存 = 新 manifest + 新 edit_revision（CAS）；**确认**（EDIT-04 流程，preview 必须）生成 ConfirmedEdit，之后草稿修改不影响它。

## 3. 支持操作（v1 冻结范围）

| 操作 | 支持 | 实现 |
| --- | --- | --- |
| 裁切 | ✅ 任意实例 `source_in/out_ms` | trim/atrim |
| 顺序拼接 | ✅ 单轨任意数量 | concat 滤镜 |
| 速度 | ✅ `0.25 ≤ speed ≤ 4.0`（0.5/2 因子链） | `setpts=(PTS-STARTPTS)/s` + atempo 链；**音画同变速**，v1 不做音画分离 |
| 实例独立参数 | ✅ 每实例 speed/区间互不影响 | 每实例独立滤镜段（R09：只改该实例） |
| 多轨并行/混音、交叉淡化、视频特效、变速不变调 | ❌ **不支持**（R09：不展示不可用的假效果） | 界面不出现对应控件；扩展为后续独立任务，先冻契约再建 UI |

- 前端时间轴按 manifest 渲染实例（`timeline_start_ms` + 有效时长 `(source_out-source_in)/speed`）；拖动=草稿编辑（保存走 CAS）；**明确插入**才写实例（R09）。

## 4. 媒体格式与存储

- **导出/交付**：MP4 = h264 (yuv420p) + aac 44.1kHz；输出尺寸 = 各源媒体最大尺寸取偶数，fps = canvas.fps。
- **接受输入**（上传与供应商产物入库时）：mp4(h264/aac)、webm(vp8|vp9/opus)、mov、m4a、wav、mp3；其余格式 422 拒绝（`validation_failed`，附扩展名）。供应商生成视频入库时**转码归一为 mp4/h264/aac**（同命令管线），原文件保留为 MediaArtifact 的 `raw` 依据。
- **私有存储**：`data/studio/media/{projectId}/{artifactId}.mp4`（ROOT-01 的 data_root 之下）；文件写入 = 临时文件 + 原子 rename；入库即算 sha256 记入 MediaArtifact。
- **公开发布访问**：与私有存储物理分离：`data/studio/public/{releaseId}/…`（仅该 Release 明确列出的产物文件，白名单路径、无目录列举）；私有原文/过程媒体永不落入公开目录（契约 §6）。观看页 `/watch/[releaseId]` 只取公开产物。

## 5. 取消 / 失败 / 重试（导出 = StudioJob，ROOT-04 定义 job 表）

| 情形 | 行为 |
| --- | --- |
| 取消 | 杀掉 ffmpeg 进程组；job → cancelled；删除临时输出；**不动源媒体/草稿** |
| 失败 | 退出码≠0 或超时（默认 10 分钟）→ job failed + stderr 尾 4KB；不产生半成品发布 |
| 重试 | **同版重试**：同一 ConfirmedEdit + output_spec 重新提交 job（R09：导出失败可同版重试，无需重新生成镜头）；供应商不可用等外部错误不得自动重复**收费**调用（ROOT-04 的幂等边界） |
| 预览失败 | media element `error` → 实例显示错误态（具体消息），不显示假画面/定时器动画 |

## 6. 预览-导出一致性（验收门槛）

- **同一 manifest** 驱动浏览器播放调度与 ffmpeg 命令推导；两侧的时间模型相同（整数毫秒、半开区间、speed 因子）。
- 服务端侧已由 PoC 证明：切点画面、总时长、分段音高与 manifest 精确一致（[证据](../../evidence/media-kernel-poc-2026-10-04.md)）。
- 浏览器侧由 GATE-04/05 用真实页面验收：实例插入→预览切点/时长与 manifest 一致、同版导出文件可播放、发布后观看页播放同一产物；**无浏览器证据保持未验证**。

## 7. 依赖与许可证登记

| 依赖 | 版本 | 许可证 | 用途 | 备注 |
| --- | --- | --- | --- | --- |
| imageio-ffmpeg | 0.6.0（已列 requirements.txt） | MIT（Python 包）/ **GPL-2.0-or-later**（所携带 ffmpeg 7.0.2 静态二进制，johnvansickle 构建） | 导出/转码/探测 | 子进程隔离，不链接；只随 venv 分发 |
| pillow | 已列 | HPND | fixture 生成（测试） | 不用于生产路径 |
| 浏览器媒体 | 无新依赖 | — | 预览 | 原生 element，无 WASM/转码库 |

- 本轮**不引入** @ffmpeg/ffmpeg、mediabunny、hls.js 等新库（无必要，避免依赖膨胀）；如后续多轨混音需要 WASM 内核，另立任务重冻本附录。
