"""统一的错误信封。

方案 9.1：错误格式为 `{"error":{"code","message","requestId"}}`；参数错误 400、
未登录 401、无权限 403、不存在或不可见 404、状态冲突 409、超限 413、限流 429。

刻意不用 FastAPI 默认的 `detail` 字段：客户端的错误分支应该按稳定的 `code`
判断，而不是按人写的 message 匹配字符串。
"""

from __future__ import annotations

from typing import Any

# HTTP 状态码到默认错误码的映射，用于框架自身抛出的 HTTPException。
DEFAULT_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "bad_request",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


class AppError(Exception):
    """业务错误。抛它，由 main.py 的异常处理器转成统一信封。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    # ---- 常用构造 ----

    @classmethod
    def bad_request(cls, message: str, code: str = "bad_request") -> "AppError":
        return cls(400, code, message)

    @classmethod
    def unauthorized(cls, message: str = "未登录", code: str = "unauthorized") -> "AppError":
        return cls(401, code, message)

    @classmethod
    def forbidden(cls, message: str = "无权限", code: str = "forbidden") -> "AppError":
        return cls(403, code, message)

    @classmethod
    def not_found(cls, message: str = "不存在", code: str = "not_found") -> "AppError":
        return cls(404, code, message)

    @classmethod
    def conflict(cls, message: str, code: str = "conflict") -> "AppError":
        return cls(409, code, message)

    @classmethod
    def too_large(cls, message: str, code: str = "payload_too_large") -> "AppError":
        return cls(413, code, message)

    @classmethod
    def rate_limited(cls, message: str = "请求过于频繁", code: str = "rate_limited") -> "AppError":
        return cls(429, code, message)

    @classmethod
    def guest_board_immutable(cls) -> "AppError":
        """对访客区调用归档 / 续期 / 恢复接口时返回（方案 9.3）。"""
        return cls(409, "guest_board_immutable", "访客区不支持归档、续期或恢复")


def error_payload(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "requestId": request_id}}
