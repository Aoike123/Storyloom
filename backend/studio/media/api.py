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
from .models import MediaArtifact
from .store import artifact_path
from .stream import (
    RangeNotSatisfiable,
    content_type_for,
    iter_file_range,
    parse_range,
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
