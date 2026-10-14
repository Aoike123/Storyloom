"""Storyloom Studio — 分镜域 HTTP 路由（/api/studio/projects/{pid}/scenes*，附录 04 §2）。

本卡只交付 SB1 新建场；SB2/SB3/SB4/SB7 由后续子卡填充。
路由层只做请求体形态校验与重放状态码切换；业务逻辑在 storyboard/service.py。
鉴权：core.auth.require_user（严格 Bearer，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from . import service

router = APIRouter(tags=["studio-storyboard"])


class CreateSceneIn(BaseModel):
    """SB1 请求体（附录 04 §2）：``name, seq?|null(= 下一位), command_id``。

    无任何片段入参（架构契约冻结：场行没有 fragment/adoption 列，片段归属
    由场内镜头的 script_adoption_id 承载，SB4 建立）。
    seq 可缺省或显式 null（= 项目内下一个空位，由 service 裁定）。
    """

    name: str
    seq: int | None = None
    command_id: str

    @field_validator("seq", mode="before")
    @classmethod
    def _seq_is_strict_int(cls, v):
        """显式 seq 的 JSON 类型守卫（附录 04 §2 + SC1 显式排除写法）。

        显式提供（非 null）的 seq 必须是 JSON 整数：bool（``true``/``false``）、
        小数（1.5）、字符串（"1"）等一律 422。Pydantic 默认宽松模式会把
        ``True`` 强制转成 1、把 ``1.5`` 截成 1，使服务层无法区分——故在
        形态层按 mode="before" 拦掉；形态层的 422 由 FastAPI 生成（本项目
        未安装 RequestValidationError 处理器），服务层仍对 int 做 rule=invalid
        复核（见 service._validate）。
        """
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("seq 必须是正整数。")
        return v


@router.post("/projects/{pid}/scenes", status_code=201)
def create_scene(
    pid: str, body: CreateSceneIn, user: User = Depends(require_user)
):
    """SB1 新建场（附录 04 §2）：成功 201 场 DTO；同 command_id 重放 200 原
    result_payload。只创建场行，不生成任何镜头。"""
    with Session() as s:
        result = service.create_scene(
            s, user, pid, body.name, body.seq, body.command_id
        )
    if result.pop("_replay", False):
        # 重放命中：200 原样返回首次 result_payload（覆盖 201）
        return JSONResponse(status_code=200, content=result)
    return result
