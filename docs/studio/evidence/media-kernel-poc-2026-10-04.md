# 剪辑内核 PoC 证据 · 2026-10-04

ROOT-03 实测记录。基线：studio-rebuild 分支（PoC 提交见 git log）；运行时 `.venv/bin/python 3.12.14`；ffmpeg `7.0.2-static`（johnvansickle 静态构建，经 imageio-ffmpeg 0.6.0 提供，已在 requirements.txt）。脚本：[scripts/media_kernel_poc.py](../../scripts/media_kernel_poc.py)。

## 素材（确定性生成，已提交 fixture）

| 文件 | 大小 | 时长 | sha256 | 内容 |
| --- | --- | --- | --- | --- |
| `tests/fixtures/studio/media/test_a.mp4` | 156,976 B | 10.000s | `08e6c37b…b1a82` | 320×240@25fps 红底(200,40,40) + 时间码文本 + 440Hz 正弦音轨（h264+aac） |
| `tests/fixtures/studio/media/test_b.mp4` | 159,461 B | 10.000s | `aca20145…03d` | 蓝底(40,40,200) + 时间码文本 + 880Hz 正弦音轨 |
| `tests/fixtures/studio/media/tone_440.m4a` | 123,104 B | 10.000s | `edffaf5e…1d8` | 440Hz 正弦（aac） |

素材由 PIL 逐帧渲染 + 纯 Python 正弦合成后以**独立临时文件**喂给 ffmpeg 编码（两个 `-i -` 共享一个 stdin 会交错读取导致像素/采样错位，脚本中已注明禁止）。

## 编辑（`poc_manifest.json`，schema `storyloom.editmanifest/v1`）

instances：`test_a.mp4 [2000,7000)ms speed 1.0` + `test_b.mp4 [1000,4000)ms speed 1.0`；期望总时长 **8.000s**。

## 导出命令（由 manifest 推导，非手写）

```text
ffmpeg -i test_a.mp4 -i test_b.mp4 -filter_complex
  "[0:v]trim=2.0:7.0,setpts=(PTS-STARTPTS)[v0];
   [0:a]atrim=2.0:7.0,asetpts=PTS-STARTPTS,atempo=1.0[a0];
   [1:v]trim=1.0:4.0,setpts=(PTS-STARTPTS)[v1];
   [1:a]atrim=1.0:4.0,asetpts=PTS-STARTPTS,atempo=1.0[a1];
   [v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
  -map [v] -map [a] -c:v libx264 -pix_fmt yuv420p -r 25 -c:a aac -ar 44100 poc_export.mp4
```

## 验证结果（实测输出，退出码 0）

| 检查 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| 总时长 | 8.000s ±0.08 | 8.000s | PASS |
| t=2.5s 画面 | A 红 (≈198,40,39) | (198,40,39) | PASS |
| t=6.5s 画面 | B 蓝 (≈39,40,199) | (39,40,199) | PASS |
| t=4.96s（切点前 40ms） | 仍为 A | (198,40,39) | PASS |
| [0,5)s 主导音高 | 440Hz | 440Hz（过零率法，rms 6930） | PASS |
| [5,8)s 主导音高 | 880Hz | 880Hz（rms 6954） | PASS |

**结论**：同一规范化 manifest 推导的导出，切点、时长、声音与 manifest 精确一致；输出是真实可播放文件（98,999 B mp4）。未使用固定时长、定时器或图标模拟。

## 限制

- 验证为服务端导出侧；浏览器播放/监看的一致性在 GATE-04/05 用真实浏览器验收（无浏览器证据前保持未验证）。
- 过零率是粗估（±40Hz 容差内与精确 440/880 正弦一致）；正式媒体一致性测试（GATE）改用采样级比对。
- 静态 ffmpeg 无 ffprobe：探测用 `ffmpeg -i` stderr 解析（工具在 `backend/infrastructure/media/` 实现时沿用此法）。
