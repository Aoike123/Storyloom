"""Storyloom Studio — 片段域 HTTP 路由（/api/studio/projects/{pid}/fragments*，附录 02 §2）。

SOURCE-03..08 逐子卡填充（F1 POST /fragments 等）。路由层只做请求体
校验与 DTO 组装；业务逻辑在 fragment_service.py。鉴权：core.auth
.require_user（严格 Bearer/sl_auth，无匿名回退）。
"""
from fastapi import APIRouter

router = APIRouter(tags=["studio-fragments"])
