"""Storyloom Studio — 来源域 HTTP 路由（/api/studio/projects/{pid}/sources*，附录 01 §2）。

SOURCE-01-d/e 逐子卡填充路由（S1 POST、S3 GET /{revisionId}）；
S2/S4/S5/S6 属 SOURCE-03+ 后续卡。路由层只做请求体校验与 DTO 组装；
业务逻辑在 sources/service.py。鉴权：core.auth.require_user。
"""
from fastapi import APIRouter

router = APIRouter(tags=["studio-sources"])
