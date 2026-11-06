"""登录、注册、退出、Discord 授权（协议 3.1）。"""

import hashlib
import json
import logging
import re
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from site_v1.config import get_config
from site_v1.deps import Ctx, get_redis, get_session_factory
from site_v1.dto import me_dto
from site_v1.errors import SiteError, forbidden, invalid, unauthenticated, unavailable
from site_v1.fluxer import (
    AccountExists,
    AccountUnavailable,
    DiscordUser,
    FluxerSession,
    FluxerUnavailable,
    InvalidCredentials,
    MfaRequired,
    Origin,
    get_fluxer,
)
from site_v1.routers.site import ensure_session
from site_v1.services.accounts import ensure_account_rows, load_viewer
from site_v1.session import SessionStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["账号"])

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OAUTH_STATE_PREFIX = "site:oauth:state:"


def request_origin(request: Request) -> Origin:
    """IP 只信 CF-Connecting-IP（协议 1.6），没有时用直连地址。"""
    ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else ""
    )
    return Origin(ip=ip, user_agent=request.headers.get("user-agent", "")[:512])


def _str(body: dict, key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) else None


async def start_authenticated_session(
    ctx, fluxer_session: FluxerSession, *, email=None, newcomer=False
):
    """登录成功后的公共步骤：懒建站内账号、轮换会话和 csrf、下发 Cookie、刷新有效模式。"""
    user_id = fluxer_session.identity.user_id
    async with get_session_factory()() as session:
        await ensure_account_rows(session, user_id, email=email, newcomer=newcomer)
        await session.commit()
    store = SessionStore(get_redis(), get_config())
    ctx.session = await store.create_authenticated(
        user_id, fluxer_session.token, replace=ctx.session
    )
    ctx.pending_cookies.append(store.cookie_header(ctx.session))
    ctx.viewer = await load_viewer(get_session_factory(), user_id)
    if ctx.viewer is None:
        raise unavailable("账号资料暂时不可用")
    ctx.refresh_mode()
    return ctx.viewer


def _login_error(exc: Exception) -> SiteError:
    if isinstance(exc, InvalidCredentials):
        return invalid({"password": "用户名或密码不对"}, "用户名或密码不对")
    if isinstance(exc, MfaRequired):
        return forbidden(
            "这个账号开启了两步验证，暂时只能用 Discord 登录", reason="mfa_required"
        )
    if isinstance(exc, AccountUnavailable):
        return forbidden("账号不可用", reason="account_unavailable")
    return unavailable("账号服务暂时不可用")


@router.post("/login", summary="登录")
async def login(request: Request, ctx: Ctx, body: dict = Body(...)):
    username = (_str(body, "username") or "").strip()
    password = _str(body, "password") or ""
    fields = {}
    if not username:
        fields["username"] = "请填写用户名或邮箱"
    if not password:
        fields["password"] = "请填写密码"
    if fields:
        raise invalid(fields)
    try:
        fluxer_session = await get_fluxer().password_login(
            username, password, request_origin(request)
        )
    except (
        InvalidCredentials,
        MfaRequired,
        AccountUnavailable,
        FluxerUnavailable,
    ) as exc:
        raise _login_error(exc) from exc
    viewer = await start_authenticated_session(ctx, fluxer_session)
    return me_dto(viewer)


@router.post("/register", summary="站内注册", status_code=201)
async def register(request: Request, ctx: Ctx, body: dict = Body(...)):
    config = get_config()
    username = _str(body, "username")
    password = _str(body, "password")
    display_name = _str(body, "displayName")
    email = _str(body, "email")
    fields = {}
    if not username or not USERNAME_RE.fullmatch(username):
        fields["username"] = "用户名只能用 3-20 位字母、数字、下划线"
    if not password or len(password) < 8:
        fields["password"] = "密码至少 8 位"
    elif len(password) > 128:
        fields["password"] = "密码最多 128 位"
    if email is not None and email != "" and not EMAIL_RE.fullmatch(email):
        fields["email"] = "邮箱格式不对"
    if display_name is not None and (
        not display_name.strip() or len(display_name.strip()) > 32
    ):
        fields["displayName"] = "昵称 1-32 个字"
    if fields:
        raise invalid(fields)
    if not config.registration_open:
        raise forbidden(
            "现在只接受 Discord 授权注册", reason="registration_discord_only"
        )
    try:
        fluxer_session = await get_fluxer().register(
            username,
            password,
            email=email or None,
            display_name=display_name.strip() if display_name else None,
            origin=request_origin(request),
        )
    except AccountExists as exc:
        message = (
            "这个用户名已被使用" if exc.field == "username" else "这个邮箱已被使用"
        )
        raise invalid({exc.field: message}) from exc
    except FluxerUnavailable as exc:
        raise unavailable("账号服务暂时不可用") from exc
    viewer = await start_authenticated_session(
        ctx, fluxer_session, email=email or None, newcomer=True
    )
    return me_dto(viewer)


@router.post("/logout", summary="退出", status_code=204)
async def logout(ctx: Ctx):
    session = ctx.session
    store = SessionStore(get_redis(), get_config())
    if session is not None:
        if session.token:
            try:
                await get_fluxer().revoke_session(session.token)
            except FluxerUnavailable:
                # Fluxer 注销失败不阻止本地退出；令牌只在服务端，删掉会话就拿不到了
                logger.warning("Fluxer 会话注销失败，本地会话照常删除")
        await store.destroy(session)
    ctx.session = None
    ctx.viewer = None
    ctx.refresh_mode()
    ctx.pending_cookies.append(store.clear_cookie_header())
    return Response(status_code=204)


# ---------------- Discord 授权 ----------------


def _discord_client() -> tuple[str, str]:
    """client_id / client_secret：优先 SITE_DISCORD_*，否则复用旧接口的 config.json auth 配置。"""
    config = get_config()
    client_id, secret = config.discord_client_id, config.discord_client_secret
    if not client_id:
        from api.v1.routers import auth as legacy_auth

        legacy = legacy_auth._AUTH_CONFIG or {}
        client_id = str(legacy.get("client_id") or "")
        secret = secret or str(legacy.get("client_secret") or "")
    if not client_id and config.fluxer_fake:
        client_id = "fake-client-id"
    if not client_id:
        raise unavailable("Discord 登录未配置")
    return client_id, secret


def _redirect_uri(request: Request) -> str:
    return get_config().discord_redirect_uri or str(
        request.url_for("site_v1_discord_callback")
    )


def _login_location(error: str | None = None) -> str:
    base = get_config().frontend_base + "/login"
    return f"{base}?{urlencode({'error': error})}" if error else base


def _redirect(location: str) -> Response:
    return Response(status_code=302, headers={"Location": location})


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


@router.get("/discord/start", summary="跳 Discord 授权")
async def discord_start(request: Request, ctx: Ctx, intent: str | None = None):
    if intent not in ("login", "link"):
        raise invalid({"intent": "intent 只能是 login 或 link"})
    if intent == "link" and ctx.viewer is None:
        raise unauthenticated("绑定 Discord 要先登录")
    client_id, _ = _discord_client()
    session = await ensure_session(ctx)
    state = secrets.token_urlsafe(32)
    await get_redis().set(
        OAUTH_STATE_PREFIX + hashlib.sha256(state.encode()).hexdigest(),
        json.dumps(
            {
                "sid": _sid_hash(session.sid),
                "intent": intent,
                "user_id": ctx.viewer.user_id if ctx.viewer else None,
            }
        ),
        ex=600,
        nx=True,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return _redirect(f"https://discord.com/oauth2/authorize?{urlencode(params)}")


_ERROR_CODE = re.compile(r"^[a-z_]{1,40}$")


async def _fetch_discord_user(request: Request, code: str) -> DiscordUser | None:
    """code → access_token → /users/@me（和 V1/V2 回调一样的两次请求）。"""
    client_id, secret = _discord_client()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        token = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(request),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token.status_code != 200:
            return None
        access_token = token.json().get("access_token")
        if not access_token:
            return None
        me = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me.status_code != 200:
            return None
        raw = me.json()
    if not raw.get("id") or not raw.get("username"):
        return None
    return DiscordUser(
        id=str(raw["id"]),
        username=str(raw["username"]),
        global_name=raw.get("global_name"),
        avatar=raw.get("avatar"),
    )


@router.get(
    "/discord/callback",
    summary="Discord 回调：登录或绑定",
    name="site_v1_discord_callback",
)
async def discord_callback(
    request: Request,
    ctx: Ctx,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """处理完一律 302 回站内 /login；出错带 ?error={错误码}（10-25 第 6 条）。"""
    if error:
        return _redirect(
            _login_location(error if _ERROR_CODE.fullmatch(error) else "discord_error")
        )
    if not code or not state or ctx.session is None:
        return _redirect(_login_location("invalid_state"))
    raw = await get_redis().getdel(
        OAUTH_STATE_PREFIX + hashlib.sha256(state.encode()).hexdigest()
    )
    saved = json.loads(raw) if raw else None
    if not saved or saved.get("sid") != _sid_hash(ctx.session.sid):
        return _redirect(_login_location("invalid_state"))
    if get_config().fluxer_fake:
        return _redirect(_login_location("discord_unavailable"))
    if saved.get("intent") == "link":
        # 绑定需要 Fluxer 提供「给已登录账号加 Discord 绑定」的 RPC（见 docs/site_v1/fluxer-rpc.md）
        return _redirect(_login_location("link_unsupported"))
    try:
        discord_user = await _fetch_discord_user(request, code)
    except (httpx.HTTPError, ValueError):
        logger.warning("Discord 授权请求失败", exc_info=True)
        discord_user = None
    if discord_user is None:
        return _redirect(_login_location("discord_failed"))
    exchange_id = hashlib.sha256(f"{state}:{discord_user.id}".encode()).hexdigest()
    try:
        fluxer_session = await get_fluxer().discord_exchange(
            discord_user, exchange_id, request_origin(request)
        )
        await start_authenticated_session(ctx, fluxer_session)
    except (FluxerUnavailable, SiteError):
        logger.warning("Discord 登录换取 Fluxer 会话失败", exc_info=True)
        return _redirect(_login_location("temporarily_unavailable"))
    return _redirect(_login_location())
