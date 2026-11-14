"""/api/v1 的 ASGI 中间件：建请求上下文、校验 CSRF、给每个响应加 X-Content-Mode。

只处理 /api/v1 下的请求，其他路径原样放过。顺序：
1. 读会话 Cookie → Redis 会话 → FluxerPort.resolve_session → 本地账号快照 → 有效模式；
   Fluxer / Redis / 数据库不可用 → 503 temporarily_unavailable（不返回 401/403）。
2. 限频（协议 1.6，site_v1/rate_limit.py）：已登录账号的写请求按账号、未登录的请求按 IP，
   超限 → 429 rate_limited + Retry-After。
3. 写请求（POST/PUT/PATCH/DELETE）校验 X-CSRF-Token，缺少或不对 → 403 forbidden。
4. 路由执行；响应头补 X-Content-Mode（按请求结束时的有效模式）和待写的 Set-Cookie。
5. 路由里未处理的异常 → 503（协议：忙时必须 503）。

身份头模式（SITE_V1_AUTH_MODE=principal，网关与核心接入约定）：第 1–3 步换成
「校验 X-Ody-Principal → 本地用户行 → 有效模式取 Principal.mode」；没有有效头一律 401，
不读会话 Cookie；会话、CSRF、限频都由网关负责，这里不再做；/auth/*、/session 返回 404。
"""

import logging

from starlette.datastructures import MutableHeaders
from starlette.requests import Request

from site_v1 import rate_limit
from site_v1.config import get_config
from site_v1.context import SiteContext
from site_v1 import principal as principals
from site_v1.context import apply_principal
from site_v1.errors import (
    SiteError,
    forbidden,
    is_site_path,
    not_found,
    render_error,
    unauthenticated,
    unavailable,
)
from site_v1.session import SessionStore

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def build_context(request: Request, ctx: SiteContext) -> None:
    """按会话 Cookie 组出 ctx（会话、账号、有效模式）。"""
    from site_v1.deps import get_redis, get_session_factory
    from site_v1.fluxer import FluxerUnavailable, get_fluxer
    from site_v1.services.accounts import load_viewer

    config = get_config()
    store = SessionStore(get_redis(), config)
    try:
        session = await store.load(request.cookies.get(config.cookie_name))
    except Exception as exc:
        raise unavailable("会话服务暂时不可用") from exc
    ctx.session = session
    if session is None or not session.user_id:
        ctx.refresh_mode()
        return
    try:
        identity = await get_fluxer().resolve_session(session.token or "")
    except FluxerUnavailable as exc:
        raise unavailable("账号服务暂时不可用") from exc
    viewer = None
    if identity is not None and identity.user_id == session.user_id:
        try:
            viewer = await load_viewer(get_session_factory(), identity.user_id)
        except SiteError:
            raise
        except Exception as exc:
            raise unavailable("账号资料暂时不可用") from exc
    if viewer is None:
        # Fluxer 会话失效或账号已合并：降为匿名会话（保留 csrf，前端不用重取）
        session.user_id = None
        session.token = None
        try:
            await store.save(session)
        except Exception as exc:
            raise unavailable("会话服务暂时不可用") from exc
    ctx.viewer = viewer
    ctx.refresh_mode()


def session_only_path(path: str) -> bool:
    """身份头模式下不提供的本地会话接口（登录、注册、退出、Discord 授权、GET /session）。"""
    path = path.rstrip("/")
    return path in ("/api/v1/session", "/api/v1/auth") or path.startswith("/api/v1/auth/")


async def build_principal_context(request: Request, ctx: SiteContext) -> None:
    """身份头模式：校验 X-Ody-Principal，按 userId 懒建本地用户行，有效模式取 Principal.mode。"""
    from site_v1 import gateway_users
    from site_v1.deps import get_redis, get_session_factory
    from site_v1.services.accounts import load_viewer

    config = get_config()
    principal = principals.verify(
        request.headers.get(principals.HEADER), config.principal_secret
    )
    if principal is None:
        raise unauthenticated("缺少或无效的身份头")
    ctx.principal = principal
    principals.set_current(principal)
    if principal.user_id:
        factory = get_session_factory()
        try:
            viewer = await load_viewer(factory, principal.user_id)
            if viewer is None:
                await gateway_users.ensure_users(
                    factory, [principal.user_id], trusted=True
                )
                viewer = await load_viewer(factory, principal.user_id)
            elif viewer.username == viewer.user_id:
                await gateway_users.refresh_placeholder(
                    factory, get_redis(), principal.user_id
                )
        except SiteError:
            raise
        except Exception as exc:
            raise unavailable("账号资料暂时不可用") from exc
        if viewer is not None:
            ctx.viewer = apply_principal(viewer, principal)
            try:
                await principals.remember(get_redis(), principal)
            except Exception:  # noqa: BLE001
                logger.info("记录 Principal 有效模式失败", exc_info=True)
    ctx.refresh_mode()


class SiteV1Middleware:
    """纯 ASGI 中间件（不用 BaseHTTPMiddleware，避免流式响应和上下文变量问题）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_site_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        ctx = SiteContext()
        scope.setdefault("state", {})["site_ctx"] = ctx
        request = Request(scope)

        async def send_error(error: SiteError):
            response = render_error(error, ctx.effective_mode)
            for cookie in ctx.pending_cookies:
                response.headers.append("set-cookie", cookie)
            await response(scope, receive, send)

        config = get_config()
        if config.principal_mode:
            reset = principals.set_current(None)
            try:
                await self._principal_request(scope, receive, send, ctx, request, send_error)
            finally:
                principals.reset_current(reset)
            return

        try:
            await build_context(request, ctx)
            await rate_limit.check(
                scope,
                request.headers,
                scope["method"],
                ctx.viewer.user_id if ctx.viewer is not None else None,
            )
            if scope["method"].upper() not in SAFE_METHODS:
                presented = request.headers.get("x-csrf-token")
                if ctx.session is None or not ctx.session.csrf_matches(presented):
                    raise forbidden("CSRF 校验失败，请刷新页面")
        except SiteError as error:
            await send_error(error)
            return
        except Exception:
            logger.exception("site_v1 建立请求上下文失败")
            await send_error(unavailable())
            return

        await self._run(scope, receive, send, ctx, send_error)

    async def _principal_request(self, scope, receive, send, ctx, request, send_error):
        try:
            if session_only_path(scope.get("path", "")):
                raise not_found("会话由网关管理")
            await build_principal_context(request, ctx)
        except SiteError as error:
            await send_error(error)
            return
        except Exception:
            logger.exception("site_v1 校验身份头失败")
            await send_error(unavailable())
            return
        await self._run(scope, receive, send, ctx, send_error)

    async def _run(self, scope, receive, send, ctx, send_error):
        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["X-Content-Mode"] = ctx.effective_mode
                if "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
                for cookie in ctx.pending_cookies:
                    headers.append("set-cookie", cookie)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "site_v1 处理请求出错 %s %s", scope["method"], scope.get("path")
            )
            if started:
                raise
            await send_error(unavailable())
