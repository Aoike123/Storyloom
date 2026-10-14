"""MEDIA-02-a 私有媒体流（冻结规格）：Content-Type 映射 + HTTP Range 语义。

与 MEDIA-01（store.py）的分工：本模块只读已登记产物，绝不写盘、绝不创建
或写入 ``public_root()``（私有/公开物理分离；公开流属 MEDIA-02-b）。所有
读取都先经 ``store.artifact_path()`` 的越界防护，路径不合法或文件缺失由路由
层转成与「artifact 不存在」同形状的 404。

Range 语义（RFC 7233 单区间子集，保守且自洽）：
- ``bytes=start-end``：闭区间；``end`` 超出文件尾按文件尾截断（仍可满足）；
- ``bytes=start-``：start 到文件尾；
- ``bytes=-N``：最后 N 字节（N ≥ 文件大小 → 全文件）；
- 不可满足：``start ≥ 文件大小``、``start > end``、``-0``、空文件上的任何 Range、
  多区间（``a-b,c-d``）、非 ``bytes`` 单位、语法非法/空。
不可满足用 ``RangeNotSatisfiable`` 表达——416 属 HTTP 传输语义，冻结码表无对应
``StudioAPIError`` 码，故不经过错误协议。
"""
from pathlib import Path

# 分块读取块大小：分段请求按 seek 后逐块产出，绝不整文件读入内存再切片。
CHUNK_SIZE = 64 * 1024

# 确定性映射：(media_kind, 小写扩展名) → MIME。扩展名优先于 kind 兜底。
_CONTENT_TYPES = {
    ("video", "mp4"): "video/mp4",
    ("video", "webm"): "video/webm",
    ("video", "mov"): "video/quicktime",
    ("audio", "mp3"): "audio/mpeg",
    ("audio", "m4a"): "audio/mp4",
    ("audio", "wav"): "audio/wav",
    ("image", "png"): "image/png",
    ("image", "jpg"): "image/jpeg",
    ("image", "jpeg"): "image/jpeg",
    ("image", "webp"): "image/webp",
    ("image", "gif"): "image/gif",
}

_FALLBACK_BY_KIND = {
    "video": "video/mp4",
    "audio": "audio/mpeg",
    "image": "image/png",
}


class RangeNotSatisfiable(Exception):
    """Range 不可满足（→ 416 + ``Content-Range: bytes */total``）。"""

    def __init__(self, total: int):
        self.total = total
        super().__init__(f"Range 不可满足（文件大小 {total} 字节）。")


def _is_digits(s: str) -> bool:
    """纯 ASCII 数字（拒绝 unicode 数字与空串）。"""
    return bool(s) and s.isascii() and s.isdigit()


def content_type_for(media_kind: str, filename: str) -> str:
    """按 media_kind + 扩展名给出确定性 Content-Type。

    已知扩展名优先；未知扩展名按 media_kind 兜底；两者都不认识 →
    ``application/octet-stream``。
    """
    ext = Path(filename).suffix.lstrip(".").lower()
    mapped = _CONTENT_TYPES.get((media_kind, ext))
    if mapped is not None:
        return mapped
    return _FALLBACK_BY_KIND.get(media_kind, "application/octet-stream")


def parse_range(header: str, total: int) -> tuple[int, int]:
    """解析单个 ``bytes`` 区间，返回闭区间 ``(start, end)``（均含端点）。

    不可满足或不受支持的形式一律 raise ``RangeNotSatisfiable(total)``。
    """
    raw = (header or "").strip()
    if not raw.lower().startswith("bytes="):
        raise RangeNotSatisfiable(total)
    spec = raw[len("bytes="):].strip()
    if spec == "" or "," in spec or "-" not in spec:
        raise RangeNotSatisfiable(total)

    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()

    if first == "":
        # 后缀式 bytes=-N：最后 N 字节。
        if not _is_digits(last):
            raise RangeNotSatisfiable(total)
        n = int(last)
        if n == 0 or total == 0:
            raise RangeNotSatisfiable(total)
        if n >= total:
            return 0, total - 1
        return total - n, total - 1

    if not _is_digits(first):
        raise RangeNotSatisfiable(total)
    start = int(first)
    if total == 0 or start >= total:
        raise RangeNotSatisfiable(total)

    if last == "":
        # 开放式 bytes=start-：到文件末尾。
        return start, total - 1

    if not _is_digits(last):
        raise RangeNotSatisfiable(total)
    end = int(last)
    if end < start:
        raise RangeNotSatisfiable(total)
    if end >= total:
        end = total - 1
    return start, end


def iter_file_range(path: Path, start: int, end: int):
    """seek 到 start 后分块产出 ``[start, end]``（含端点）的字节。

    ``CHUNK_SIZE`` 分块，内存占用与请求区间同阶（不整文件读取）。
    """
    remaining = end - start + 1
    if remaining <= 0:
        return
    with open(path, "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk
