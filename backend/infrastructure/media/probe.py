"""MEDIA-01 媒体探测：只用静态 ffmpeg（无 ffprobe）解析 stderr。

imageio_ffmpeg 分发的是 johnvansickle 静态 ffmpeg（ffprobe 不可用），因此
`ffmpeg -i <path>` 的 stderr 是唯一可用的元数据来源：

- `Duration: HH:MM:SS.cc` → duration_ms；
- `Stream #..: Video: ..., 640x480 [SAR ..]` → width/height（先剔除 0x.. 编码十六进制，避免误匹配）；
- `Stream #..: Video:` / `Stream #..: Audio:` → has_video / has_audio。

无法解析出任何流（非媒体、损坏、空文件）时一律 raise MediaProbeError，
绝不返回猜测/伪数据（"无法探测" 必须对调用方可见）。
"""
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

# ffmpeg -i 的探测不需要产出，超时上限（挂死的输入不得阻塞请求）
PROBE_TIMEOUT_SECONDS = 30

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_DIMENSIONS_RE = re.compile(r"(\d{1,5})x(\d{1,5})")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")


class MediaProbeError(Exception):
    """媒体探测失败：文件不可读、不是媒体，或元数据无法解析。"""


def ffmpeg_binary() -> str:
    """静态 ffmpeg 可执行文件绝对路径（由 imageio_ffmpeg 分发）。"""
    return imageio_ffmpeg.get_ffmpeg_exe()


def _has_stream(stderr: str, kind: str) -> bool:
    """stderr 中是否存在 `Stream #0:0...: Video:` / `Audio:` 形式的流声明。"""
    needle = f": {kind}:"
    for line in stderr.splitlines():
        if needle in line and line.lstrip().startswith("Stream #"):
            return True
    return False


def _parse_dimensions(stderr: str) -> tuple[int | None, int | None]:
    """取第一条 Video 流的分辨率；音频/无视频流 → (None, None)。"""
    for line in stderr.splitlines():
        stripped = line.lstrip()
        if ": Video:" not in line or not stripped.startswith("Stream #"):
            continue
        cleaned = _HEX_RE.sub("", line)
        m = _DIMENSIONS_RE.search(cleaned)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None, None
    return None, None


def _parse_duration_ms(stderr: str) -> int | None:
    """`Duration: 00:00:01.00` → 1000；缺省或 N/A → None。"""
    m = _DURATION_RE.search(stderr)
    if not m:
        return None
    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return int(round((hours * 3600 + minutes * 60 + seconds) * 1000))


def probe_media(path) -> dict:
    """探测媒体文件，返回恰好 5 键：duration_ms/width/height/has_video/has_audio。

    :raises MediaProbeError: 文件不存在/不可读、ffmpeg 无法启动或超时、
        无法解析出任何音视频流（非媒体/损坏）。
    """
    target = Path(path)
    if not target.is_file():
        raise MediaProbeError(f"媒体文件不存在或不可读：{target}")

    try:
        proc = subprocess.run(
            [ffmpeg_binary(), "-i", str(target)],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError(f"媒体探测超时：{target}") from exc
    except OSError as exc:
        raise MediaProbeError(f"媒体探测无法执行：{target}") from exc

    stderr = proc.stderr or ""
    has_video = _has_stream(stderr, "Video")
    has_audio = _has_stream(stderr, "Audio")
    if not has_video and not has_audio:
        raise MediaProbeError(f"不是可识别的媒体文件或文件已损坏：{target}")

    width, height = _parse_dimensions(stderr) if has_video else (None, None)
    return {
        "duration_ms": _parse_duration_ms(stderr),
        "width": width,
        "height": height,
        "has_video": has_video,
        "has_audio": has_audio,
    }
