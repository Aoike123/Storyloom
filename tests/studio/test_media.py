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
from backend.studio.media import stream as stream_mod
from backend.studio.media.stream import (
    RangeNotSatisfiable,
    content_type_for,
    iter_file_range,
    parse_range,
)
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


# ══════════════════════════════════════════════════════════════════════════
# MEDIA-02-a 私有媒体流（GET /api/studio/projects/{pid}/media/{artifact_id}）
# 严格属主 + Range；猜 ID 不读他人文件；纯读零副作用、不写 public_root()。
# 以下为追加用例，上方 MEDIA-01 用例原样保留。
# ══════════════════════════════════════════════════════════════════════════
def _register(client, username: str) -> str:
    r = client.post(
        "/api/studio/auth/register",
        json={"username": username, "password": "secret123", "email": "t@x"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_project(client, token: str, name: str = "MediaProj") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": make_ulid()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_via_api(client, username: str, name: str = "MediaProj"):
    """注册用户 + 建其项目，返回 (token, pid)。"""
    token = _register(client, username)
    return token, _mk_project(client, token, name)


def _put_artifact(
    pid: str,
    data: bytes = b"0123456789" * 4,
    *,
    media_kind: str = "video",
    ext: str = "mp4",
    artifact_id: str | None = None,
):
    """直接落一个小文件到 media_root() 并登记 MediaArtifact 行，返回 (id, path)。

    直接插行（不经 store_media）以便自由选择扩展名且不触发 ffmpeg 探测；
    MEDIA-01 的真实落盘路径另由 test_media_stored_artifact_streams 覆盖。
    """
    aid = artifact_id or make_ulid()
    rel = f"{pid}/{aid}.{ext}"
    path = media_root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with Session() as s:
        s.add(
            MediaArtifact(
                id=aid,
                project_id=pid,
                media_kind=media_kind,
                filename=rel,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                source="upload",
            )
        )
        s.commit()
    return aid, path


def _media_url(pid: str, aid: str) -> str:
    return f"/api/studio/projects/{pid}/media/{aid}"


def _media_rows_snapshot():
    """全部 MediaArtifact 行的列值快照（纯读零副作用断言用）。"""
    with Session() as s:
        return sorted(
            (
                a.id,
                a.project_id,
                a.media_kind,
                a.filename,
                a.size_bytes,
                a.sha256,
                a.duration_ms,
                a.width,
                a.height,
                a.source,
                a.source_job_id,
            )
            for a in s.execute(select(MediaArtifact)).scalars().all()
        )


def _public_snapshot():
    """public_root() 的存在性与文件集（用于纯读零副作用断言）。"""
    root = public_root()
    files: set[str] = set()
    if root.exists():
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                files.add(os.path.join(dirpath, name))
    return (root.exists(), frozenset(files))


# ── Content-Type 确定性映射（单元） ─────────────────────────────────────
def test_stream_content_type_deterministic():
    pid = make_ulid()
    assert content_type_for("video", f"{pid}/a.mp4") == "video/mp4"
    assert content_type_for("video", f"{pid}/a.webm") == "video/webm"
    assert content_type_for("video", f"{pid}/a.MOV") == "video/quicktime"
    assert content_type_for("audio", f"{pid}/a.mp3") == "audio/mpeg"
    assert content_type_for("audio", f"{pid}/a.m4a") == "audio/mp4"
    assert content_type_for("audio", f"{pid}/a.WAV") == "audio/wav"
    assert content_type_for("image", f"{pid}/a.png") == "image/png"
    assert content_type_for("image", f"{pid}/a.jpg") == "image/jpeg"
    assert content_type_for("image", f"{pid}/a.jpeg") == "image/jpeg"
    assert content_type_for("image", f"{pid}/a.webp") == "image/webp"
    assert content_type_for("image", f"{pid}/a.gif") == "image/gif"
    # 未知扩展名按 media_kind 兜底；kind/扩展名都不认识 → octet-stream
    assert content_type_for("video", f"{pid}/a.xyz") == "video/mp4"
    assert content_type_for("audio", f"{pid}/noext") == "audio/mpeg"
    assert content_type_for("image", f"{pid}/a.bmp") == "image/png"
    assert content_type_for("document", f"{pid}/a.pdf") == "application/octet-stream"


# ── Range 解析（单元） ──────────────────────────────────────────────────
def test_stream_parse_range_supported_forms():
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=5-", 100) == (5, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=-100", 100) == (0, 99)  # N == 文件大小 → 全文件
    assert parse_range("bytes=-1000", 100) == (0, 99)  # N > 文件大小 → 全文件
    assert parse_range("bytes=95-1000", 100) == (95, 99)  # end 越界按文件尾截断
    assert parse_range("bytes=99-99", 100) == (99, 99)
    assert parse_range("BYTES=0-0", 100) == (0, 0)  # 单位大小写不敏感
    assert parse_range(" bytes=1-2 ", 100) == (1, 2)  # 首尾空白容忍


@pytest.mark.parametrize(
    "bad",
    [
        "bytes=100-",  # start >= 文件大小
        "bytes=100-200",
        "bytes=10-5",  # start > end
        "bytes=0-1,3-4",  # 多区间（不支持，保守判不可满足）
        "bytes=abc-def",  # 语法非法
        "bytes=",
        "bytes=--5",
        "items=0-5",  # 非 bytes 单位
        "0-9",
        "",
    ],
)
def test_stream_parse_range_unsatisfiable(bad):
    with pytest.raises(RangeNotSatisfiable) as ei:
        parse_range(bad, 100)
    assert ei.value.total == 100


def test_stream_parse_range_empty_file_unsatisfiable():
    for header in ("bytes=0-", "bytes=0-0", "bytes=-1"):
        with pytest.raises(RangeNotSatisfiable):
            parse_range(header, 0)


def test_stream_iter_file_range_seeks_and_chunks(tmp_path, monkeypatch):
    """分段读取：从 start seek 起分块产出，只产出区间字节（不整文件）。"""
    data = bytes(range(256)) * 40  # 10240 字节
    blob = tmp_path / "blob.bin"
    blob.write_bytes(data)
    monkeypatch.setattr(stream_mod, "CHUNK_SIZE", 4)
    chunks = list(iter_file_range(blob, 100, 109))
    assert b"".join(chunks) == data[100:110]
    assert [len(c) for c in chunks] == [4, 4, 2]
    assert all(len(c) <= 4 for c in chunks)
    assert list(iter_file_range(blob, 5, 4)) == []  # 空区间零产出


# ── 鉴权与属主（HTTP） ──────────────────────────────────────────────────
def test_media_t1_no_token_401(db, client):
    token, pid = _seed_via_api(client, "media_t1")
    aid, _ = _put_artifact(pid)
    r = client.get(_media_url(pid, aid))
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


def test_media_t2_project_missing_404_kind_project(db, client):
    token, pid = _seed_via_api(client, "media_t2")
    aid, _ = _put_artifact(pid)
    missing = make_ulid()
    r = client.get(_media_url(missing, aid), headers=_auth(token))
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": missing}


def test_media_t3_other_owner_project_404_kind_project(db, client):
    token_a, pid_a = _seed_via_api(client, "media_t3_a")
    secret = b"T3-SECRET-BYTES" * 3
    aid_a, _ = _put_artifact(pid_a, secret)
    token_b = _register(client, "media_t3_b")

    r = client.get(_media_url(pid_a, aid_a), headers=_auth(token_b))
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": pid_a}
    assert secret not in r.content


def test_media_t4_guess_artifact_id_not_leaked(db, client):
    """核心：猜中他人 artifact_id 也只得到与「不存在」逐字节同形状的 404。"""
    token_a, pid_a = _seed_via_api(client, "media_t4_a")
    secret = b"T4-TOP-SECRET-CONTENT-" + b"A" * 64
    aid_a, _ = _put_artifact(pid_a, secret)

    token_b = _register(client, "media_t4_b")
    pid_b = _mk_project(client, token_b, "T4B")

    r_guess = client.get(_media_url(pid_b, aid_a), headers=_auth(token_b))
    missing = make_ulid()
    r_missing = client.get(_media_url(pid_b, missing), headers=_auth(token_b))

    for r in (r_guess, r_missing):
        assert r.status_code == 404, r.text
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"]["kind"] == "media_artifact"
        assert set(err["details"]) == {"kind", "id"}
    assert r_guess.json()["error"]["details"]["id"] == aid_a
    assert secret not in r_guess.content
    # 逐字节同形状：两响应唯一差异是被请求的 artifact id（同为 26 字符 ULID）
    assert len(aid_a) == len(missing)
    assert len(r_guess.content) == len(r_missing.content)
    assert r_guess.content.replace(aid_a.encode(), b"<ID>") == r_missing.content.replace(
        missing.encode(), b"<ID>"
    )


def test_media_t5_cross_project_same_owner_404_media_artifact(db, client):
    token, pid1 = _seed_via_api(client, "media_t5")
    pid2 = _mk_project(client, token, "T5Second")
    data = b"T5-P1-ONLY-DATA" * 4
    aid1, _ = _put_artifact(pid1, data)

    r = client.get(_media_url(pid2, aid1), headers=_auth(token))
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "media_artifact", "id": aid1}
    assert data not in r.content


# ── 正常返回与 Range ────────────────────────────────────────────────────
def test_media_t6_no_range_200_full_body(db, client):
    token, pid = _seed_via_api(client, "media_t6")
    data = bytes(range(256)) * 3
    aid, _ = _put_artifact(pid, data)

    r = client.get(_media_url(pid, aid), headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.headers["accept-ranges"] == "bytes"
    assert int(r.headers["content-length"]) == len(data)
    assert r.content == data
    assert r.headers["content-type"] == "video/mp4"


def test_media_t7_range_first_ten_206(db, client):
    token, pid = _seed_via_api(client, "media_t7")
    data = b"0123456789abcdefghij"  # 20 字节
    aid, _ = _put_artifact(pid, data)

    r = client.get(_media_url(pid, aid), headers={**_auth(token), "Range": "bytes=0-9"})
    assert r.status_code == 206, r.text
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-range"] == f"bytes 0-9/{len(data)}"
    assert int(r.headers["content-length"]) == 10  # 不得等于全文件长度
    assert r.content == data[:10]


def test_media_t8_range_open_ended_to_eof_206(db, client):
    token, pid = _seed_via_api(client, "media_t8")
    data = bytes(range(64))
    aid, _ = _put_artifact(pid, data)

    r = client.get(_media_url(pid, aid), headers={**_auth(token), "Range": "bytes=5-"})
    assert r.status_code == 206, r.text
    assert r.headers["content-range"] == f"bytes 5-63/{len(data)}"
    assert int(r.headers["content-length"]) == len(data) - 5
    assert r.content == data[5:]


def test_media_t9_range_suffix_last_n_206(db, client):
    token, pid = _seed_via_api(client, "media_t9")
    data = bytes(range(64))
    aid, _ = _put_artifact(pid, data)

    r = client.get(_media_url(pid, aid), headers={**_auth(token), "Range": "bytes=-4"})
    assert r.status_code == 206, r.text
    assert r.headers["content-range"] == f"bytes 60-63/{len(data)}"
    assert int(r.headers["content-length"]) == 4
    assert r.content == data[-4:]


def test_media_t10_range_end_beyond_total_clamped_206(db, client):
    token, pid = _seed_via_api(client, "media_t10")
    data = b"x" * 32
    aid, _ = _put_artifact(pid, data)

    r = client.get(
        _media_url(pid, aid), headers={**_auth(token), "Range": "bytes=30-9999"}
    )
    assert r.status_code == 206, r.text
    assert r.headers["content-range"] == "bytes 30-31/32"
    assert int(r.headers["content-length"]) == 2
    assert r.content == data[30:]


@pytest.mark.parametrize(
    "bad",
    [
        "bytes=32-",  # start >= 文件大小
        "bytes=40-50",
        "bytes=10-5",  # start > end
        "bytes=abc",
        "bytes=",
        "bytes=0-1,5-6",  # 多区间不受支持
        "items=0-5",  # 非 bytes 单位
        "bytes=-0",
    ],
)
def test_media_t11_unsatisfiable_range_416(db, client, bad):
    token, pid = _seed_via_api(client, "media_t11")
    aid, _ = _put_artifact(pid, b"x" * 32)

    r = client.get(_media_url(pid, aid), headers={**_auth(token), "Range": bad})
    assert r.status_code == 416, (bad, r.text)
    assert r.headers["content-range"] == "bytes */32"
    assert r.headers["accept-ranges"] == "bytes"


def test_media_t12_empty_file_200_and_416(db, client):
    token, pid = _seed_via_api(client, "media_t12")
    aid, _ = _put_artifact(pid, b"")

    r = client.get(_media_url(pid, aid), headers=_auth(token))
    assert r.status_code == 200, r.text
    assert int(r.headers["content-length"]) == 0
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == b""

    r2 = client.get(_media_url(pid, aid), headers={**_auth(token), "Range": "bytes=0-"})
    assert r2.status_code == 416, r2.text
    assert r2.headers["content-range"] == "bytes */0"


# ── 磁盘缺文件 / 越界行：与不存在同形状 ────────────────────────────────
def test_media_t13_missing_file_on_disk_404_same_shape(db, client):
    token, pid = _seed_via_api(client, "media_t13")
    aid, path = _put_artifact(pid, b"T13-GONE")
    path.unlink()

    r = client.get(_media_url(pid, aid), headers=_auth(token))
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "media_artifact", "id": aid}

    missing = make_ulid()
    r2 = client.get(_media_url(pid, missing), headers=_auth(token))
    assert r2.status_code == 404
    assert r.content.replace(aid.encode(), b"<ID>") == r2.content.replace(
        missing.encode(), b"<ID>"
    )


def test_media_t14_traversal_filename_404_no_read(db, client):
    """登记行文件名越界（../）：404 同形状，绝不读取私有根之外的文件。"""
    token, pid = _seed_via_api(client, "media_t14")
    evil_body = b"EVIL-OUTSIDE-PRIVATE-ROOT"
    evil = media_root().parent / "media-stream-evil.bin"
    evil.write_bytes(evil_body)
    aid = make_ulid()
    try:
        with Session() as s:
            s.add(
                MediaArtifact(
                    id=aid,
                    project_id=pid,
                    media_kind="video",
                    filename="../media-stream-evil.bin",
                    size_bytes=len(evil_body),
                    sha256=hashlib.sha256(evil_body).hexdigest(),
                    source="upload",
                )
            )
            s.commit()
        r = client.get(_media_url(pid, aid), headers=_auth(token))
        assert r.status_code == 404, r.text
        assert r.json()["error"]["details"] == {"kind": "media_artifact", "id": aid}
        assert evil_body not in r.content
    finally:
        try:
            evil.unlink()
        except OSError:
            pass


# ── 纯读零副作用 / 公开根隔离 / 真实落盘行 ──────────────────────────────
def test_media_t15_pure_read_zero_side_effects(db, client):
    token_a, pid_a = _seed_via_api(client, "media_t15_a")
    token_b, pid_b = _seed_via_api(client, "media_t15_b")
    data = bytes(range(256))
    aid, path = _put_artifact(pid_a, data)

    rows_before = _artifact_count()
    media_rows_before = _media_rows_snapshot()
    files_before = _all_media_files()
    public_before = _public_snapshot()

    for _ in range(3):
        assert (
            client.get(_media_url(pid_a, aid), headers=_auth(token_a)).status_code == 200
        )
        assert (
            client.get(
                _media_url(pid_a, aid), headers={**_auth(token_a), "Range": "bytes=0-9"}
            ).status_code
            == 206
        )
        assert (
            client.get(
                _media_url(pid_a, aid), headers={**_auth(token_a), "Range": "bytes=9999-"}
            ).status_code
            == 416
        )
        assert (
            client.get(_media_url(pid_b, aid), headers=_auth(token_b)).status_code == 404
        )

    assert _artifact_count() == rows_before
    assert _media_rows_snapshot() == media_rows_before  # 行内容零改动
    assert _all_media_files() == files_before
    assert _public_snapshot() == public_before
    # 私有文件未被改动、且不在公开根之下
    assert path.read_bytes() == data
    assert not path.resolve().is_relative_to(public_root().resolve())


def test_media_t16_stored_artifact_streams_range(db, client):
    """真实 store_media 落盘行：分段内容正确、Content-Type 与映射函数一致。"""
    token, pid = _seed_via_api(client, "media_t16")
    payload = b"\x89PNG-stored-image-bytes" * 4
    public_before = _public_snapshot()
    with Session() as s:
        art = store_media(
            s,
            project_id=pid,
            filename="cover.mp4",
            data=payload,
            media_kind="image",
            source="export",
        )
        s.commit()
        aid = art.id
        expected_type = content_type_for(art.media_kind, art.filename)

    r = client.get(_media_url(pid, aid), headers={**_auth(token), "Range": "bytes=2-6"})
    assert r.status_code == 206, r.text
    assert r.headers["content-type"] == expected_type
    assert r.headers["content-range"] == f"bytes 2-6/{len(payload)}"
    assert r.content == payload[2:7]

    full = client.get(_media_url(pid, aid), headers=_auth(token))
    assert full.status_code == 200
    assert full.content == payload
    assert _public_snapshot() == public_before  # 公开根零改动
