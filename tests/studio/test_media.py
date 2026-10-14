"""MEDIA-01 私有媒体落盘与完整性验收测试。

覆盖：文件名安全/扩展名白名单/大小/类型/来源校验（均 422 且零落盘零落行）、
真实小视频落盘与探测、篡改后完整性失败、损坏媒体不落行、
原子写失败无残留、路径穿越拒止、私有根与公开根物理分离。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
db fixture 照抄 tests/studio/test_projects.py，users 行最小列照抄 test_source_models.py。
本文件不 import 任何旧后端模块。
"""
import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select, text

from backend.core.db import Base, Session, engine, make_ulid
from backend.infrastructure.media.probe import (
    MediaProbeError,
    ffmpeg_binary,
    probe_media,
)
from backend.studio.contracts.errors import StudioAPIError
from backend.studio.media import store as store_mod
from backend.studio.media.models import MediaArtifact
from backend.studio.media.store import (
    ALLOWED_EXTENSIONS,
    MAX_MEDIA_BYTES,
    artifact_path,
    media_root,
    public_root,
    safe_filename,
    store_media,
    verify_integrity,
)
from backend.studio.projects.models import StudioProject

_ULID_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]{26}$")


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（projects↔source_revisions）：drop 前临时关 FK
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(conn, checkfirst=True)
        conn.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(conn)
        conn.commit()
    finally:
        conn.close()
    yield
    engine.dispose()


# ── 辅助 ────────────────────────────────────────────────────────────────
def _seed_project(owner_id: str = "u-media") -> str:
    """建 users 行 + project 行（MediaArtifact.project_id 有 FK），返回 project_id。"""
    pid = make_ulid()
    with Session() as s:
        s.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
                " VALUES (:id, :uname, :email, :ph, :at, :cr)"
            ),
            {
                "id": owner_id,
                "uname": owner_id,
                "email": f"{owner_id}@t",
                "ph": "x",
                "at": "t",
                "cr": 0.0,
            },
        )
        s.add(StudioProject(id=pid, owner_id=owner_id, name=f"media-{pid}"))
        s.commit()
    return pid


def _all_media_files() -> set[str]:
    """私有根下全部文件（含隐藏临时文件）的绝对路径集合。"""
    root = media_root()
    found: set[str] = set()
    if not root.exists():
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.add(os.path.join(dirpath, name))
    return found


def _artifact_count() -> int:
    with Session() as s:
        return len(s.execute(select(MediaArtifact)).scalars().all())


def _expect_validation(exc: Exception, field: str, rule: str) -> None:
    assert isinstance(exc, StudioAPIError)
    assert exc.code == "validation_failed"
    assert exc.status == 422
    violations = exc.details["violations"]
    assert any(
        v["field"] == field and v["rule"] == rule for v in violations
    ), f"未命中 {field}/{rule}: {violations}"


def _generate_video(tmp_path: Path) -> bytes:
    """用 imageio_ffmpeg 的静态 ffmpeg 生成 1 秒 64x64 小视频。"""
    out = tmp_path / "src.mp4"
    subprocess.run(
        [
            ffmpeg_binary(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert out.is_file() and out.stat().st_size > 0
    return out.read_bytes()


# ── 冻结常量与路径分离 ──────────────────────────────────────────────────
def test_frozen_constants():
    assert ALLOWED_EXTENSIONS == ("mp4", "webm", "mov", "m4a", "wav", "mp3")
    assert MAX_MEDIA_BYTES == 512 * 1024 * 1024
    assert media_root() == media_root().resolve()


def test_media_root_separated_from_public_root():
    """私有/公开物理分离：私有根不落在公开根之下（且本卡不写公开根）。"""
    assert media_root().resolve().is_relative_to(public_root().resolve()) is False
    assert public_root() == public_root().parent / "public"
    assert "media" in media_root().parts and "public" not in media_root().parts


# ── 文件名安全 ──────────────────────────────────────────────────────────
def test_safe_filename_accepts_plain_name():
    assert safe_filename("clip.mp4") == "clip.mp4"
    assert safe_filename("a b_c-1.WAV") == "a b_c-1.WAV"


@pytest.mark.parametrize(
    "bad",
    ["", "../x.mp4", "a/b.mp4", "..\\x.mp4", "/abs/x.mp4", "\\abs\\x.mp4", "x\x00.mp4", ".."],
)
def test_safe_filename_rejects_unsafe(bad):
    with pytest.raises(StudioAPIError) as ei:
        safe_filename(bad)
    _expect_validation(ei.value, "filename", "invalid")


@pytest.mark.parametrize("bad", ["../x.mp4", "a/b.mp4", "/abs/x.mp4", "x\\y.mp4", "..\\x.mp4"])
def test_store_media_traversal_no_write_no_row(db, bad):
    """路径穿越/非法文件名：422 且零落盘零落行。"""
    pid = _seed_project()
    before = _all_media_files()
    with pytest.raises(StudioAPIError) as ei:
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename=bad,
                data=b"data",
                media_kind="video",
                source="upload",
            )
    _expect_validation(ei.value, "filename", "invalid")
    assert _artifact_count() == 0
    assert _all_media_files() == before


# ── 扩展名白名单 ────────────────────────────────────────────────────────
def test_store_media_unsupported_extension(db):
    pid = _seed_project()
    before = _all_media_files()
    with pytest.raises(StudioAPIError) as ei:
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="clip.avi",
                data=b"0123456789",
                media_kind="video",
                source="upload",
            )
    _expect_validation(ei.value, "filename", "unsupported_type")
    assert "avi" in ei.value.details["violations"][0]["message"]
    assert _artifact_count() == 0
    assert _all_media_files() == before


def test_store_media_no_extension_rejected(db):
    pid = _seed_project()
    before = _all_media_files()
    with pytest.raises(StudioAPIError) as ei:
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="clip",
                data=b"0123456789",
                media_kind="video",
                source="upload",
            )
    _expect_validation(ei.value, "filename", "unsupported_type")
    assert _artifact_count() == 0
    assert _all_media_files() == before


# ── 大小 / 类型 / 来源 ──────────────────────────────────────────────────
def test_store_media_empty_data(db):
    pid = _seed_project()
    before = _all_media_files()
    with pytest.raises(StudioAPIError) as ei:
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="clip.mp4",
                data=b"",
                media_kind="video",
                source="upload",
            )
    _expect_validation(ei.value, "size", "empty")
    assert _artifact_count() == 0
    assert _all_media_files() == before


def test_store_media_too_large(db, monkeypatch):
    """调小上限触发 too_large，不写真大文件。"""
    pid = _seed_project()
    monkeypatch.setattr(store_mod, "MAX_MEDIA_BYTES", 16)
    before = _all_media_files()
    with pytest.raises(StudioAPIError) as ei:
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="clip.mp4",
                data=b"x" * 17,
                media_kind="video",
                source="upload",
            )
    _expect_validation(ei.value, "size", "too_large")
    assert _artifact_count() == 0
    assert _all_media_files() == before


def test_store_media_invalid_kind(db):
    pid = _seed_project()
    before = _all_media_files()
    with pytest.raises(StudioAPIError) as ei:
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="clip.mp4",
                data=b"0123456789",
                media_kind="document",
                source="upload",
            )
    _expect_validation(ei.value, "media_kind", "invalid")
    assert _artifact_count() == 0
    assert _all_media_files() == before


def test_store_media_invalid_source(db):
    pid = _seed_project()
    before = _all_media_files()
    with pytest.raises(StudioAPIError) as ei:
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="clip.mp4",
                data=b"0123456789",
                media_kind="video",
                source="scrape",
            )
    _expect_validation(ei.value, "source", "invalid")
    assert _artifact_count() == 0
    assert _all_media_files() == before


# ── 探测（独立单元） ────────────────────────────────────────────────────
def test_probe_media_missing_file(tmp_path):
    with pytest.raises(MediaProbeError):
        probe_media(tmp_path / "nope.mp4")


def test_probe_media_garbage_raises(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(os.urandom(4096))
    with pytest.raises(MediaProbeError):
        probe_media(bad)


def test_probe_media_returns_exactly_five_keys(tmp_path):
    """冻结返回体：恰好 5 键，且 duration/width/height 为真实解析值。"""
    data = _generate_video(tmp_path)
    video = tmp_path / "src.mp4"
    info = probe_media(video)
    assert set(info) == {"duration_ms", "width", "height", "has_video", "has_audio"}
    assert len(info) == 5
    assert info["has_video"] is True
    assert info["has_audio"] is False
    assert (info["width"], info["height"]) == (64, 64)
    assert info["duration_ms"] is not None and info["duration_ms"] > 0
    assert len(data) > 0


# ── 真实媒体落盘 ────────────────────────────────────────────────────────
def test_store_media_real_video(db, tmp_path):
    pid = _seed_project()
    data = _generate_video(tmp_path)
    with Session() as s:
        art = store_media(
            s,
            project_id=pid,
            filename="clip.mp4",
            data=data,
            media_kind="video",
            source="upload",
        )
        s.commit()

        assert _ULID_RE.match(art.id), art.id
        assert art.filename == f"{pid}/{art.id}.mp4"
        assert art.filename.split("/") == [pid, f"{art.id}.mp4"]
        assert art.project_id == pid
        assert art.media_kind == "video"
        assert art.source == "upload"
        assert art.size_bytes == len(data)
        assert art.sha256 == hashlib.sha256(data).hexdigest()
        assert art.duration_ms is not None and art.duration_ms > 0
        assert (art.width, art.height) == (64, 64)

        path = artifact_path(art)
        assert path.is_file()
        assert path.is_relative_to(media_root().resolve())
        assert path == (media_root() / art.filename).resolve()
        assert not path.is_relative_to(public_root().resolve())
        assert verify_integrity(art) is True


def test_store_media_image_kind_not_probed(db):
    """image 不探测：duration/width/height 三者 None，仍成功落行。"""
    pid = _seed_project()
    fake = b"\x89PNG-not-really-an-image"
    with Session() as s:
        art = store_media(
            s,
            project_id=pid,
            filename="cover.MP4",
            data=fake,
            media_kind="image",
            source="export",
        )
        s.commit()
        assert art.filename.endswith(".mp4")  # 扩展名归一化后落盘
        assert art.duration_ms is None and art.width is None and art.height is None
        assert art.size_bytes == len(fake)
        assert verify_integrity(art) is True


# ── 完整性 ──────────────────────────────────────────────────────────────
def test_verify_integrity_false_after_tamper(db, tmp_path):
    pid = _seed_project()
    data = _generate_video(tmp_path)
    with Session() as s:
        art = store_media(
            s,
            project_id=pid,
            filename="clip.mp4",
            data=data,
            media_kind="video",
            source="upload",
        )
        s.commit()
        path = artifact_path(art)
        assert verify_integrity(art) is True

        # 同长度不同内容 → sha256 不一致
        path.write_bytes(b"\x00" * len(data))
        assert path.stat().st_size == art.size_bytes
        assert verify_integrity(art) is False

        # 长度变化 → size 不一致
        path.write_bytes(data + b"tail")
        assert verify_integrity(art) is False

        # 文件缺失
        path.unlink()
        assert verify_integrity(art) is False
        assert _artifact_count() == 1  # 篡改不影响登记行本身


def test_corrupt_media_no_row_no_residue(db, tmp_path):
    """损坏文件：probe_media 抛错，store_media 不留行、不留文件（含临时文件）。"""
    pid = _seed_project()
    garbage = os.urandom(4096)
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(garbage)
    with pytest.raises(MediaProbeError):
        probe_media(bad)

    before = _all_media_files()
    with pytest.raises(MediaProbeError):
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="broken.mp4",
                data=garbage,
                media_kind="video",
                source="upload",
            )
    assert _artifact_count() == 0
    assert _all_media_files() == before


def test_atomic_write_failure_no_row_no_file(db, tmp_path, monkeypatch):
    pid = _seed_project()
    data = _generate_video(tmp_path)
    before = _all_media_files()

    def _boom(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr(store_mod.os, "replace", _boom)
    with pytest.raises(OSError):
        with Session() as s:
            store_media(
                s,
                project_id=pid,
                filename="clip.mp4",
                data=data,
                media_kind="video",
                source="upload",
            )
    assert _artifact_count() == 0
    assert _all_media_files() == before  # 临时文件已清理，无残留


# ── artifact_path 越界拒止 ──────────────────────────────────────────────
def test_artifact_path_rejects_escape():
    pid = make_ulid()
    for evil in ("../evil.mp4", "/tmp/evil.mp4", "../../etc/passwd"):
        art = MediaArtifact(
            id=make_ulid(),
            project_id=pid,
            media_kind="video",
            filename=evil,
            size_bytes=1,
            sha256="0" * 64,
            source="upload",
        )
        with pytest.raises(ValueError):
            artifact_path(art)
        assert verify_integrity(art) is False
