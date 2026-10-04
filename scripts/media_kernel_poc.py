#!/usr/bin/env python3
"""ROOT-03 剪辑内核 PoC。

流程：
  1. 生成两段确定性时间码视频（红/蓝底 + 时间码文本 + 440/880Hz 正弦音轨）
     与一个音频文件，输出到 tests/fixtures/studio/media/。
  2. 由规范化 EditManifest 推导 ffmpeg 命令（预览与导出共用同一 manifest）。
  3. 执行导出，验证：总时长、切点画面（色）、每段主导音高（过零率）。

退出码 0 = 全部验证通过；非 0 = 失败。证据日志打印到 stdout。
"""
from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import imageio_ffmpeg  # noqa: E402  （venv 已装；定位静态 ffmpeg 二进制）

FFMPEG = Path(imageio_ffmpeg.get_ffmpeg_exe())
MEDIA = ROOT / "tests/fixtures/studio/media"
OUT = MEDIA / "poc_export.mp4"

W, H, FPS, SR = 320, 240, 25, 44100
DUR_S = 10.0


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], data: bytes | None = None) -> bytes:
    p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"cmd failed ({p.returncode}): {' '.join(cmd[:6])}...\n{p.stderr.decode(errors='replace')[-2000:]}")
    return p.stdout


def sine_wave(freq: float, seconds: float, amp: float = 0.3) -> bytes:
    n = int(seconds * SR)
    pcm = bytearray()
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / SR))
        pcm += struct.pack("<h", v)
    return bytes(pcm)


def gen_fixture(name: str, bg: tuple[int, int, int], label: str, freq: float) -> Path:
    """10s 时间码视频：纯色底 + 'label HH:MM:SS.mmm' 文本 + 正弦音轨。

    帧与音频写入独立临时文件后交给 ffmpeg——禁止两个 `-i -` 共享一个 stdin
    （会交错读取导致像素/采样错位）。
    """
    import tempfile

    from PIL import Image, ImageDraw

    path = MEDIA / name
    if path.exists() and path.stat().st_size > 0:
        return path
    total = int(DUR_S * FPS)
    with tempfile.TemporaryDirectory() as td:
        frames = Path(td) / "frames.raw"
        audio = Path(td) / "audio.raw"
        with frames.open("wb") as fo:
            for f in range(total):
                t = f / FPS
                img = Image.new("RGB", (W, H), bg)
                d = ImageDraw.Draw(img)
                tc = f"{label} {t/3600:02.0f}:{t/60 % 60:02.0f}:{t % 60:06.3f}"
                d.text((12, 12), tc, fill=(255, 255, 255))
                d.text((12, H - 30), f"frame {f}/{total}", fill=(255, 255, 255))
                fo.write(img.tobytes())
        audio.write_bytes(sine_wave(freq, DUR_S))
        run([
            str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", str(frames),
            "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", str(audio),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(path),
        ])
    return path


def manifest_a_b() -> dict:
    """两段实例顺序拼接：A[2s,7s) + B[1s,4s)，期望总时长 8.0s。"""
    return {
        "schema": "storyloom.editmanifest/v1",
        "canvas": {"width": W, "height": H, "fps": FPS},
        "instances": [
            {"id": "inst-1", "media": "test_a.mp4", "source_in_ms": 2000, "source_out_ms": 7000, "speed": 1.0, "timeline_start_ms": 0},
            {"id": "inst-2", "media": "test_b.mp4", "source_in_ms": 1000, "source_out_ms": 4000, "speed": 1.0, "timeline_start_ms": 5000},
        ],
        "output": {"container": "mp4", "video": "h264", "audio": "aac", "width": W, "height": H, "fps": FPS, "sample_rate": SR},
    }


def derive_ffmpeg_cmd(m: dict) -> list[str]:
    """EditManifest → ffmpeg 命令（preview 与 export 共用此推导）。"""
    insts = m["instances"]
    inputs: list[str] = []
    fc: list[str] = []
    concat = []
    for i, ins in enumerate(insts):
        inputs += ["-i", str(MEDIA / ins["media"])]
        tin, tout = ins["source_in_ms"] / 1000, ins["source_out_ms"] / 1000
        sp = ins.get("speed", 1.0)
        fc.append(f"[{i}:v]trim=start={tin}:end={tout},setpts=(PTS-STARTPTS)/{sp}[v{i}]")
        fc.append(f"[{i}:a]atrim=start={tin}:end={tout},asetpts=PTS-STARTPTS,atempo={sp}[a{i}]")
        concat += [f"[v{i}]", f"[a{i}]"]
    fc.append("".join(concat) + f"concat=n={len(insts)}:v=1:a=1[v][a]")
    out = m["output"]
    return [
        str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", *inputs,
        "-filter_complex", ";".join(fc),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(out["fps"]),
        "-c:a", "aac", "-ar", str(out["sample_rate"]),
        str(OUT),
    ]


def probe_duration(path: Path) -> float:
    p = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = p.stderr.decode(errors="replace")
    for line in err.splitlines():
        if "Duration" in line:
            hms = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = hms.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise RuntimeError("no duration in probe output")


def frame_at(path: Path, t: float) -> tuple[float, float, float]:
    raw = run([
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-ss", f"{t:.3f}", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ])
    px = struct.unpack(f"<{W*H*3}B", raw[: W * H * 3])
    n = W * H
    return (sum(px[0::3]) / n, sum(px[1::3]) / n, sum(px[2::3]) / n)


def audio_segments(path: Path) -> tuple[dict, dict]:
    raw = run([
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-f", "s16le", "-ar", str(SR), "-ac", "1", "-",
    ])
    samples = struct.unpack(f"<{len(raw)//2}h", raw)
    out = {}
    for seg, (a, b) in {"head": (0, 5), "tail": (5, 8)}.items():
        s = samples[int(a * SR): int(b * SR)]
        zc = sum(1 for i in range(1, len(s)) if (s[i - 1] < 0 <= s[i] or s[i - 1] >= 0 > s[i]))
        rms = math.sqrt(sum(x * x for x in s) / len(s))
        # 正弦波每秒过零 2f 次 → f = zc / (2 * 秒数)
        out[seg] = {"freq_est": zc / (2 * (b - a)), "rms": rms}
    return out


def main() -> int:
    MEDIA.mkdir(parents=True, exist_ok=True)
    log("== 1. 生成/复用 fixtures ==")
    fa = gen_fixture("test_a.mp4", (200, 40, 40), "A", 440.0)
    fb = gen_fixture("test_b.mp4", (40, 40, 200), "B", 880.0)
    tone = MEDIA / "tone_440.m4a"
    if not tone.exists():
        run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-",
             "-c:a", "aac", "-b:a", "96k", str(tone)], sine_wave(440.0, DUR_S))
    for f in (fa, fb, tone):
        log(f"  {f.name}: {f.stat().st_size} bytes, duration={probe_duration(f):.3f}s")

    log("== 2. manifest 推导导出命令 ==")
    m = manifest_a_b()
    (MEDIA / "poc_manifest.json").write_text(json.dumps(m, indent=2))
    cmd = derive_ffmpeg_cmd(m)
    log("  " + " ".join(cmd))

    log("== 3. 导出并验证 ==")
    run(cmd)
    dur = probe_duration(OUT)
    log(f"  输出: {OUT.stat().st_size} bytes, duration={dur:.3f}s")

    results = []
    def check(name: str, ok: bool, detail: str) -> None:
        results.append(ok)
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    check("总时长≈8.0s", abs(dur - 8.0) < 0.08, f"measured={dur:.3f}")
    r1, g1, b1 = frame_at(OUT, 2.5)
    check("切点前=[A红色]", r1 > 120 and g1 < 110 and b1 < 110, f"rgb=({r1:.0f},{g1:.0f},{b1:.0f})")
    r2, g2, b2 = frame_at(OUT, 6.5)
    check("切点后=[B蓝色]", b2 > 120 and r2 < 110 and g2 < 110, f"rgb=({r2:.0f},{g2:.0f},{b2:.0f})")
    r3, g3, b3 = frame_at(OUT, 4.96)
    check("切点边界前0.04s仍=A", r3 > 120 and b3 < 110, f"rgb=({r3:.0f},{g3:.0f},{b3:.0f})")
    au = audio_segments(OUT)
    check("前段音高≈440Hz", abs(au["head"]["freq_est"] - 440) < 40 and au["head"]["rms"] > 500, f"est={au['head']['freq_est']:.0f}Hz rms={au['head']['rms']:.0f}")
    check("后段音高≈880Hz", abs(au["tail"]["freq_est"] - 880) < 40 and au["tail"]["rms"] > 500, f"est={au['tail']['freq_est']:.0f}Hz rms={au['tail']['rms']:.0f}")

    log("== 结果 ==")
    log(f"  {'全部通过' if all(results) else '存在失败'} ({sum(results)}/{len(results)})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
