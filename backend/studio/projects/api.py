"""Storyloom Studio — 项目域 HTTP 路由（/api/studio/projects*，附录 01 §2）。

SOURCE-01-a..c 逐子卡填充路由（P1 POST /projects、P3 GET /projects/{pid}、
P2 GET /projects）。路由层只做请求体校验与 DTO 组装；业务逻辑在
projects/service.py。鉴权：core.auth.require_user（严格 Bearer/sl_auth，
无匿名回退）。
"""
from fastapi import APIRouter

router = APIRouter(tags=["studio-projects"])
