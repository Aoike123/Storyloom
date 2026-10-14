"""MEDIA-02-a 私有媒体流路由（GET /projects/{pid}/media/{artifact_id}）。

冻结校验序（按序短路，全部纯读）：
1. 无有效 token → 401 unauthenticated（require_user 提供）；
2. 项目不存在 / 非属主 → 404 not_found{kind:project,id:pid}；
3. MediaArtifact 不存在 / 跨项目 → 404 not_found{kind:media_artifact,id}；
   （两情形同形状，不泄漏 artifact 存在性——本卡核心安全要求）
4. 登记文件不在磁盘（含 artifact_path 越界拒止）→ 404 同形状；
5. 无 Range → 200 全量；有 Range → 206 分段；不可满足 → 416。

416 属 HTTP 传输语义（冻结码表无对应 StudioAPIError 码），由本层直接返回
``Response(status_code=416)`` 并设置 ``Content-Range: bytes */total``。
读取一律走 stream.iter_file_range（seek + 分块），不整文件读入内存。
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from ..contracts.errors import StudioAPIError
from ..projects.models import StudioProject
from ..publishing.models import Release
from .models import MediaArtifact
from .store import artifact_path
from .stream import (
    RangeNotSatisfiable,
    content_type_for,
    iter_file_range,
    parse_range,
    public_content_type,
    resolve_public_path,
)

router = APIRouter(tags=["studio-media"])


@router.get("/projects/{pid}/media/{artifact_id}")
def get_media(
    pid: str,
    artifact_id: str,
    request: Request,
    user: User = Depends(require_user),
):
    """私有媒体读取：严格属主 + Accept-Ranges/Range（206/416）。纯读零副作用。"""
    with Session() as s:
        project = s.scalar(select(StudioProject).where(StudioProject.id == pid))
        if project is None or project.owner_id != user.id:
            raise StudioAPIError.not_found("project", pid)

        artifact = s.scalar(
            select(MediaArtifact).where(MediaArtifact.id == artifact_id)
        )
        if artifact is None or artifact.project_id != pid:
            # 不存在与跨项目同形状：猜 ID 不泄漏存在性。
            raise StudioAPIError.not_found("media_artifact", artifact_id)

        try:
            path = artifact_path(artifact)
        except ValueError:
            # 越界文件名：与不存在同形状，绝不读取私有根之外的文件。
            raise StudioAPIError.not_found("media_artifact", artifact_id) from None
        if not path.is_file():
            raise StudioAPIError.not_found("media_artifact", artifact_id)

        total = path.stat().st_size
        media_type = content_type_for(artifact.media_kind, artifact.filename)
        range_header = request.headers.get("range")

        if range_header is None:
            return StreamingResponse(
                iter_file_range(path, 0, total - 1),
                status_code=200,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(total),
                    "Content-Type": media_type,
                },
            )

        try:
            start, end = parse_range(range_header, total)
        except RangeNotSatisfiable:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{total}",
                },
            )

        return StreamingResponse(
            iter_file_range(path, start, end),
            status_code=206,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Type": media_type,
            },
        )


# ══════════════════════════════════════════════════════════════════════════
# MEDIA-02-b 公开发布媒体流（GET /projects/{pid}/releases/{rid}/media/{filename:path}）
#
# 「公开」严格限定为该 Release 自己的公开目录 public_root()/{public_dir}/ 内
# 已存在的普通文件——不是直接暴露私有媒体根。访问控制与 MEDIA-02-a 一致：
# 要求登录 + 严格属主，一切否定分支（项目非属主 / release 不存在或跨项目 /
# draft 未发布 / 文件名形状非法 / 路径穿越 / 文件不存在 / 目录）都用与
# 「release 不存在」逐字节同形状的 404 not_found{kind:release,id:rid}，
# 不泄漏存在性、不返回目录清单。Range 语义复用 MEDIA-02-a 的
# parse_range/iter_file_range（200/206/416）。本路由纯读零副作用：
# 不写 public_root()、不读 media_root()、不改任何 Release/MediaArtifact 行。
# ══════════════════════════════════════════════════════════════════════════
@router.get("/projects/{pid}/releases/{rid}/media/{filename:path}")
def get_release_media(
    pid: str,
    rid: str,
    filename: str,
    request: Request,
    user: User = Depends(require_user),
):
    """公开目录内文件读取：严格属主 + 仅 published/retired + Accept-Ranges/Range。"""
    with Session() as s:
        project = s.scalar(select(StudioProject).where(StudioProject.id == pid))
        if project is None or project.owner_id != user.id:
            raise StudioAPIError.not_found("project", pid)

        release = s.scalar(select(Release).where(Release.id == rid))
        if release is None or release.project_id != pid:
            # 不存在与跨项目同形状：猜 releaseId 不泄漏存在性。
            raise StudioAPIError.not_found("release", rid)
        if release.status not in ("published", "retired"):
            # 未发布（draft）不可读：防草稿泄漏，且与不存在同形状。
            raise StudioAPIError.not_found("release", rid)

        path = resolve_public_path(release.public_dir, rid, filename)
        if path is None or not path.is_file():
            # 形状非法/穿越/不存在/目录（含以 / 结尾）一律同形状 404，
            # 绝不读取 media_root()，也绝不返回目录清单。
            raise StudioAPIError.not_found("release", rid)

        total = path.stat().st_size
        media_type = public_content_type(path.name)
        range_header = request.headers.get("range")

        if range_header is None:
            return StreamingResponse(
                iter_file_range(path, 0, total - 1),
                status_code=200,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(total),
                    "Content-Type": media_type,
                },
            )

        try:
            start, end = parse_range(range_header, total)
        except RangeNotSatisfiable:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{total}",
                },
            )

        return StreamingResponse(
            iter_file_range(path, start, end),
            status_code=206,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Type": media_type,
            },
        )
