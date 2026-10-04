"""冻结错误协议：统一响应体 {"error": {"code","message","details"}}。

code 白名单与 HTTP 映射见附录 00 §4；本模块提供异常类型与 FastAPI
异常处理器安装函数。业务代码只应 raise StudioAPIError（或其工厂），
不应直接 raise HTTPException（旧风格），以保证所有错误体形状一致。
"""
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

STATUS_BY_CODE = {
    "unauthenticated": 401,
    "forbidden": 403,
    "not_found": 404,
    "validation_failed": 422,
    "range_invalid": 422,
    "range_overlap": 409,
    "revision_conflict": 409,
    "preview_stale": 409,
    "precondition_failed": 422,
    "duplicate_scope": 422,
    "duplicate": 409,  # v2.6：属主级身份唯一冲突（附录 00 §4）
    "internal_error": 500,
}


class StudioAPIError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)

    @property
    def status(self) -> int:
        return STATUS_BY_CODE[self.code]

    # ── 工厂 ──
    @classmethod
    def unauthenticated(cls, message: str = "请先登录。") -> "StudioAPIError":
        return cls("unauthenticated", message)

    @classmethod
    def forbidden(cls, kind: str, id: str, message: str = "无权访问该对象。") -> "StudioAPIError":
        return cls("forbidden", message, {"kind": kind, "id": id})

    @classmethod
    def not_found(cls, kind: str, id: str, message: str = "对象不存在或不属于当前项目。") -> "StudioAPIError":
        return cls("not_found", message, {"kind": kind, "id": id})

    @classmethod
    def validation_failed(cls, violations: list[dict[str, str]], message: str = "请求校验失败。") -> "StudioAPIError":
        return cls("validation_failed", message, {"violations": violations})

    @classmethod
    def range_invalid(cls, rule: str, message: str) -> "StudioAPIError":
        return cls("range_invalid", message, {"rule": rule})

    @classmethod
    def range_overlap(cls, conflicts: list[dict[str, Any]], message: str) -> "StudioAPIError":
        return cls("range_overlap", message, {"conflicts": conflicts})

    @classmethod
    def revision_conflict(cls, kind: str, id: str, expected, actual, message: str = "对象版本已变化，请刷新后重试。") -> "StudioAPIError":
        return cls("revision_conflict", message, {"object": {"kind": kind, "id": id}, "expected": expected, "actual": actual})

    @classmethod
    def preview_stale(cls, preview_id: str, message: str = "预览基准已变化，请重新预览。") -> "StudioAPIError":
        return cls("preview_stale", message, {"preview_id": preview_id})

    @classmethod
    def precondition_failed(cls, reason: str, blocked_by: dict[str, Any], message: str) -> "StudioAPIError":
        return cls("precondition_failed", message, {"reason": reason, "blocked_by": blocked_by})

    @classmethod
    def duplicate_scope(cls, scope_ref: str, message: str = "完整制作范围已提交过同一覆盖。") -> "StudioAPIError":
        return cls("duplicate_scope", message, {"scope_ref": scope_ref})

    @classmethod
    def duplicate(cls, scope_ref: str, message: str = "当前账号下已存在同身份对象。") -> "StudioAPIError":
        """v2.6 新码：属主级身份唯一冲突（如项目名 (owner, name) UQ）。

        与 duplicate_scope（422，覆盖范围重复提交）语义不互换（附录 00 §4
        v2.6 语义区分注）。
        """
        return cls("duplicate", message, {"scope_ref": scope_ref})

    @classmethod
    def internal(cls, message: str = "服务器内部错误，请重试。") -> "StudioAPIError":
        return cls("internal_error", message)


def error_response(e: StudioAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=e.status,
        content={"error": {"code": e.code, "message": e.message, "details": e.details}},
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StudioAPIError, lambda _req, e: error_response(e))
    # 兜底：未预期的异常 → internal_error 冻结体（不泄露内部细节）
    def _unhandled(_req, e: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "服务器内部错误，请重试。",
                    "details": {},
                }
            },
        )

    app.add_exception_handler(Exception, _unhandled)
