"""MEDIA-01 私有媒体落盘与完整性（冻结规格）。

存储布局（附录 08 §4）：私有根 `DATA_DIR/media/{project_id}/{artifact_id}.{ext}`。
公开根 `DATA_DIR/public` 只用于分离性断言，本模块永不写入/创建它——
私有/公开物理分离是硬性要求（不挂任何公开静态目录）。

写序（失败即无伪就绪记录）：
1. 文件名安全校验 → 扩展名白名单 → 大小/类型/来源校验（只 raise StudioAPIError）；
2. 同目录临时文件写入 → flush → fsync → os.replace（原子落盘）；
3. video/audio 用 ffmpeg 探测真实时长/分辨率，探测失败删除文件且不落行；
4. session.add + flush，返回行、不 commit（事务归调用方）；
5. 落盘或 flush 任一异常：删除已写文件、不留行。
"""
import hashlib
import os
from pathlib import Path

from ...core.db import DATA_DIR, make_ulid
from ...infrastructure.media.probe import MediaProbeError, probe_media
from ..contracts.errors import StudioAPIError
from .models import MediaArtifact

ALLOWED_EXTENSIONS = ("mp4", "webm", "mov", "m4a", "wav", "mp3")
MAX_MEDIA_BYTES = 512 * 1024 * 1024

_MEDIA_KINDS = ("image", "video", "audio")
_SOURCES = ("upload", "asset_generate", "shot_generate", "vendor_normalized", "export")
_PROBED_KINDS = ("video", "audio")


def media_root() -> Path:
    """私有媒体根：DATA_DIR/media（DATA_DIR 由 backend.core.db 冻结并保证存在）。"""
    return DATA_DIR / "media"


def public_root() -> Path:
    """公开根：DATA_DIR/public。仅用于分离性断言，本模块绝不写入。"""
    return DATA_DIR / "public"


def _violation(field: str, rule: str, message: str) -> StudioAPIError:
    return StudioAPIError.validation_failed(
        [{"field": field, "rule": rule, "message": message}]
    )


def safe_filename(filename: str) -> str:
    """校验并返回纯文件名（basename）。

    空、绝对路径、含 `/`、`\\`、`..` 或 NUL → validation_failed(field=filename, rule=invalid)。
    拒绝分隔符与父目录引用即可保证相对名不会被用来逃出私有根。
    """
    if not isinstance(filename, str) or filename == "":
        raise _violation("filename", "invalid", "文件名不能为空。")
    if "\x00" in filename:
        raise _violation("filename", "invalid", "文件名不能包含 NUL 字符。")
    if filename.startswith("/") or filename.startswith("\\"):
        raise _violation("filename", "invalid", "文件名必须是相对文件名，不能是绝对路径。")
    if len(filename) >= 2 and filename[1] == ":":
        raise _violation("filename", "invalid", "文件名必须是相对文件名，不能是绝对路径。")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise _violation("filename", "invalid", "文件名不能包含路径分隔符或上级目录引用。")
    return Path(filename).name


def _write_atomic(dest: Path, artifact_id: str, data: bytes) -> None:
    """同目录临时文件 → flush → fsync → os.replace；失败清理临时文件后 re-raise。"""
    tmp = dest.parent / f".{artifact_id}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def store_media(
    session,
    *,
    project_id: str,
    filename: str,
    data: bytes,
    media_kind: str,
    source: str,
    source_job_id: str | None = None,
) -> MediaArtifact:
    """校验并原子落盘，登记 MediaArtifact 行（flush，不 commit）。"""
    name = safe_filename(filename)
    ext = Path(name).suffix.lstrip(".").lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise _violation(
            "filename",
            "unsupported_type",
            f"不支持的媒体文件扩展名：.{ext}；允许：{', '.join(ALLOWED_EXTENSIONS)}。",
        )

    if not data:
        raise _violation("size", "empty", "媒体数据不能为空。")
    if len(data) > MAX_MEDIA_BYTES:
        raise _violation(
            "size", "too_large", f"媒体数据超过上限 {MAX_MEDIA_BYTES} 字节。"
        )
    if media_kind not in _MEDIA_KINDS:
        raise _violation("media_kind", "invalid", f"非法 media_kind：{media_kind!r}。")
    if source not in _SOURCES:
        raise _violation("source", "invalid", f"非法 source：{source!r}。")

    artifact_id = make_ulid()
    rel = f"{project_id}/{artifact_id}.{ext}"
    dest = media_root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    wrote = False
    try:
        _write_atomic(dest, artifact_id, data)
        wrote = True

        duration_ms = width = height = None
        if media_kind in _PROBED_KINDS:
            probe = probe_media(dest)  # 探测失败 → 由外层清理文件，不落行
            duration_ms = probe["duration_ms"]
            width = probe["width"]
            height = probe["height"]

        artifact = MediaArtifact(
            id=artifact_id,
            project_id=project_id,
            media_kind=media_kind,
            filename=rel,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            duration_ms=duration_ms,
            width=width,
            height=height,
            source=source,
            source_job_id=source_job_id,
        )
        session.add(artifact)
        session.flush()
    except BaseException:
        if wrote:
            try:
                dest.unlink()
            except OSError:
                pass
        raise
    return artifact


def artifact_path(artifact: MediaArtifact) -> Path:
    """解析私有文件绝对路径；越出私有根 → ValueError（防路径穿越）。"""
    root = media_root().resolve()
    path = (media_root() / artifact.filename).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"媒体文件名越出私有存储根：{artifact.filename!r}")
    return path


def verify_integrity(artifact: MediaArtifact) -> bool:
    """文件存在 ∧ 大小与登记一致 ∧ 重算 sha256 与登记一致。"""
    try:
        path = artifact_path(artifact)
    except ValueError:
        return False
    if not path.is_file():
        return False
    if path.stat().st_size != artifact.size_bytes:
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == artifact.sha256
