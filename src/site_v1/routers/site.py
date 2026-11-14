"""GET /site、GET /session（协议 3.1）。"""

from fastapi import APIRouter, Request
from sqlalchemy import select

from site_v1.config import get_config
from site_v1.deps import Ctx, get_redis, get_session_factory
from site_v1.dto import session_me_dto
from site_v1.session import SessionStore

router = APIRouter(tags=["账号"])

TAG_CATALOG_SETTING = "tag_catalog"


async def tag_catalog_version() -> str:
    """tag 目录版本号：site_setting[tag_catalog].version，种子脚本和 tag 管理写入。"""
    from models.site_account import SiteSetting

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(SiteSetting).where(SiteSetting.key == TAG_CATALOG_SETTING)
            )
        ).scalar_one_or_none()
    return str((row.value or {}).get("version") or "0") if row else "0"


@router.get("/site", summary="功能开关、条款版本、上传限制、tag 目录版本号")
async def get_site():
    config = get_config()
    return {
        # 协议 0.5：可选能力清单是字符串数组（0.4.1 的 {名字: 布尔} 废弃）。
        # 旧开关名：discordLogin、r18Mode、registration（能站内注册）、registrationDiscordOnly；
        # 可选能力：presence（在线状态）
        "features": [
            "discordLogin",
            "r18Mode",
            "registration" if config.registration_open else "registrationDiscordOnly",
            "presence",
        ],
        "registrationMode": config.registration_mode,
        "termsVersion": config.terms_version,
        "uploadLimits": {
            "imageMaxBytes": config.image_max_bytes,
            "fileMaxBytes": config.file_max_bytes,
            "allowedMimes": list(config.allowed_mimes),
        },
        "tagCatalogVersion": await tag_catalog_version(),
    }


async def ensure_session(ctx, request: Request | None = None):
    """没有会话时建匿名会话并下发 Cookie（GET /session、Discord 授权入口用）。"""
    if ctx.session is None:
        store = SessionStore(get_redis(), get_config())
        ctx.session = await store.create_anonymous()
        ctx.pending_cookies.append(store.cookie_header(ctx.session))
    return ctx.session


@router.get("/session", summary="是否登录、csrfToken、简要 Me")
async def get_session(ctx: Ctx):
    session = await ensure_session(ctx)
    return {
        "authenticated": ctx.viewer is not None,
        "csrfToken": session.csrf,
        "me": session_me_dto(ctx.viewer) if ctx.viewer else None,
    }
