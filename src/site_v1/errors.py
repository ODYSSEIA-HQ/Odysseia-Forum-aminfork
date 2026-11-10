"""协议 1.5 错误格式 {error:{code,message,details}}，只对 /api/v1 生效。

路由里统一 raise SiteError（或下面的快捷函数）；复用 V2 服务时它们抛的
HTTPException 按状态码映射成协议错误码，504 统一成 503。
"""

from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

API_PREFIX = "/api/v1"

# 状态码 → 默认错误码
_STATUS_CODES = {
    400: "validation_failed",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "conflict",
    422: "validation_failed",
    429: "rate_limited",
}
_DEFAULT_MESSAGES = {
    "unauthenticated": "请先登录",
    "forbidden": "没有权限",
    "not_found": "不存在或当前模式下不可见",
    "validation_failed": "参数不对",
    "conflict": "状态已变化，请刷新后重试",
    "rate_limited": "操作太频繁，请稍后再试",
    "temporarily_unavailable": "服务暂时繁忙，请稍后重试",
}
DEFAULT_RETRY_AFTER = 2


class SiteError(Exception):
    """协议错误。status 是 HTTP 状态码，code 是协议 ErrorCode。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        *,
        retry_after: int | None = None,
    ):
        self.status = status
        self.code = code
        self.message = message or _DEFAULT_MESSAGES.get(code, code)
        self.details = details
        if retry_after is None and code in {"rate_limited", "temporarily_unavailable"}:
            retry_after = DEFAULT_RETRY_AFTER
        self.retry_after = retry_after
        super().__init__(f"{status} {code}: {self.message}")


# ---------------- 快捷函数（raise 它们的返回值） ----------------


def unauthenticated(message: str = "请先登录") -> SiteError:
    return SiteError(401, "unauthenticated", message)


def forbidden(
    message: str = "没有权限", reason: str | None = None, **details
) -> SiteError:
    if reason:
        details["reason"] = reason
    return SiteError(403, "forbidden", message, details or None)


def not_found(message: str = "不存在或当前模式下不可见") -> SiteError:
    return SiteError(404, "not_found", message)


def invalid(fields: dict[str, str], message: str = "参数不对", **details) -> SiteError:
    """422 validation_failed，fields 是 字段名 → 原因。"""
    return SiteError(422, "validation_failed", message, {"fields": fields, **details})


def conflict(message: str = "状态已变化，请刷新后重试", **details) -> SiteError:
    return SiteError(409, "conflict", message, details or None)


def unavailable(
    message: str = "服务暂时繁忙，请稍后重试", retry_after: int = DEFAULT_RETRY_AFTER
) -> SiteError:
    return SiteError(503, "temporarily_unavailable", message, retry_after=retry_after)


def restricted_access_required(unmet: list[str]) -> SiteError:
    return SiteError(
        403,
        "restricted_access_required",
        "你的账号还不能开启 R18 模式",
        {"unmet": unmet},
    )


# ---------------- 渲染 ----------------


def error_payload(error: SiteError) -> dict:
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.details is not None:
        body["details"] = error.details
    return {"error": body}


def error_headers(error: SiteError, mode: str = "general") -> dict[str, str]:
    headers = {"X-Content-Mode": mode, "Cache-Control": "no-store"}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return headers


def render_error(error: SiteError, mode: str = "general") -> JSONResponse:
    return JSONResponse(
        error_payload(error),
        status_code=error.status,
        headers=error_headers(error, mode),
    )


def from_http_exception(exc: StarletteHTTPException) -> SiteError:
    """V2 服务抛的 HTTPException → 协议错误。502/503/504/5xx 一律 503。"""
    status = exc.status_code
    detail = exc.detail
    message = detail if isinstance(detail, str) and detail else None
    details = None
    code = None
    if isinstance(detail, dict):
        code = detail.get("code") if detail.get("code") in _DEFAULT_MESSAGES else None
        message = detail.get("message") or detail.get("detail") or message
        details = {
            k: v for k, v in detail.items() if k not in {"code", "message", "detail"}
        } or None
    if status >= 500:
        retry = None
        if exc.headers and exc.headers.get("Retry-After", "").isdigit():
            retry = int(exc.headers["Retry-After"])
        return SiteError(503, "temporarily_unavailable", message, retry_after=retry)
    if status == 405:
        return SiteError(404, "not_found", "不存在")
    code = code or _STATUS_CODES.get(
        status, "validation_failed" if status < 500 else "temporarily_unavailable"
    )
    if code == "validation_failed":
        details = {"fields": {}, **(details or {})}
        status = 422
    if code == "rate_limited":
        retry = exc.headers.get("Retry-After") if exc.headers else None
        return SiteError(
            429,
            code,
            message,
            details,
            retry_after=int(retry) if retry and retry.isdigit() else None,
        )
    return SiteError(status, code, message, details)


_TYPE_MESSAGES = {
    "missing": "必填",
    "string_type": "应该是文字",
    "bool_type": "只能是 true 或 false",
    "int_type": "应该是整数",
    "int_parsing": "应该是整数",
    "list_type": "应该是列表",
    "dict_type": "应该是对象",
    "model_type": "应该是对象",
    "enum": "取值不对",
    "literal_error": "取值不对",
    "string_pattern_mismatch": "格式不对",
    "string_too_short": "太短",
    "string_too_long": "太长",
    "json_invalid": "不是合法的 JSON",
    "value_error": "取值不对",
}


def _field_name(loc: tuple) -> str:
    parts = [
        str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")
    ]
    return ".".join(parts) if parts else (str(loc[0]) if loc else "body")


def from_validation_error(exc: RequestValidationError) -> SiteError:
    """RequestValidationError → 422 validation_failed + details.fields。"""
    fields: dict[str, str] = {}
    for err in exc.errors():
        name = _field_name(tuple(err.get("loc") or ()))
        if name in fields:
            continue
        ctx = err.get("ctx") or {}
        message = ctx.get("site_message") if isinstance(ctx, dict) else None
        if not message and err.get("type") == "value_error":
            raw = str(err.get("msg", ""))
            message = raw.removeprefix("Value error, ") or None
        fields[name] = message or _TYPE_MESSAGES.get(err.get("type", ""), "取值不对")
    return invalid(fields)


# ---------------- 只对 /api/v1 生效的异常处理器 ----------------


def is_site_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def _mode_of(request: Request) -> str:
    ctx = getattr(request.state, "site_ctx", None)
    return getattr(ctx, "effective_mode", "general") if ctx else "general"


def install_exception_handlers(app) -> None:
    """注册异常处理器：/api/v1 下的请求按协议格式，其他路径交回 FastAPI 默认处理。"""
    from fastapi.exception_handlers import (
        http_exception_handler,
        request_validation_exception_handler,
    )

    async def on_site_error(request: Request, exc: SiteError):
        return render_error(exc, _mode_of(request))

    async def on_http(request: Request, exc: StarletteHTTPException):
        if not is_site_path(request.url.path):
            return await http_exception_handler(request, exc)
        return render_error(from_http_exception(exc), _mode_of(request))

    async def on_validation(request: Request, exc: RequestValidationError):
        if not is_site_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        return render_error(from_validation_error(exc), _mode_of(request))

    app.add_exception_handler(SiteError, on_site_error)
    app.add_exception_handler(StarletteHTTPException, on_http)
    app.add_exception_handler(HTTPException, on_http)
    app.add_exception_handler(RequestValidationError, on_validation)
