"""V2 原生认证与当前 Discord 访问资格。"""

from fastapi import Request, HTTPException
from sqlalchemy import select
from core.tag_access_service import TagAccessService
from identity.auth_runtime import resolve_principal
from identity.fluxer_preferences_client import FluxerPreferencesClient
from models.discord_identity import DiscordIdentity
from models.user import User
from shared.database import AsyncSessionFactory
from shared.abyss_tag_visibility import can_view_abyss_tags


_permission_service = None
_PREFERENCES_STATE_KEY = "verified_fluxer_nsfw_preferences"


async def current_member(request: Request):
    """只接受 Fluxer Bearer，使用当前绑定与实时成员资格。"""
    from identity.runtime import request_identity_mode

    request_identity_mode.set("fluxer")
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "需要 Fluxer 登录")
    from api.v1.dependencies.security import VERIFIED_JWT_PAYLOAD_STATE_KEY

    sensitive = request.method not in (
        "GET",
        "HEAD",
        "OPTIONS",
    ) and not request.url.path.endswith("/view")
    sensitive = (
        sensitive
        or request.url.path.startswith("/v2/admin/")
        or request.url.path.endswith("/modlog")
    )
    principal = request.scope.get("state", {}).get(VERIFIED_JWT_PAYLOAD_STATE_KEY)
    if principal is None or sensitive:
        principal = await resolve_principal(
            header[7:].strip(), force_authority=sensitive
        )
    if not principal:
        raise HTTPException(401, "会话无效")
    # 中间件的身份快照只用于识别，不能携带旧偏好或可变权限到下一请求。
    principal = dict(principal)
    async with AsyncSessionFactory() as session:
        binding = (
            (
                await session.execute(
                    select(DiscordIdentity).where(
                        DiscordIdentity.user_id == principal["id"],
                        DiscordIdentity.is_current.is_(True),
                    )
                )
            )
            .scalars()
            .first()
        )
        profile = await session.get(User, principal["id"])
        if profile and profile.merged_into_id:
            raise HTTPException(401, "账户已合并，请重新登录")
        if binding is None:
            raise HTTPException(403, "需要当前 Discord 绑定")
        if str(principal.get("discord_user_id") or "") != binding.discord_user_id:
            raise HTTPException(503, "权威绑定投影正在同步，请稍后重试")
        principal["discord_user_id"] = binding.discord_user_id
    from api.v2.config import get_work_config

    config = get_work_config()
    access = TagAccessService(config)
    global _permission_service
    if _permission_service is None:
        from work.work_permission_service import WorkPermissionService

        _permission_service = WorkPermissionService(config)
    member = await _permission_service.member(
        principal["id"], binding.discord_user_id, binding.version, sensitive=sensitive
    )
    roles = {str(role) for role in member["roles"]}
    required = config.get("auth", {}).get("role_ids", [])
    required = required.split(",") if isinstance(required, str) else required
    if not required or not roles.intersection(str(role).strip() for role in required):
        raise HTTPException(403, "需要 Discord 正式成员身份组")
    principal["is_admin"] = str(config.get("management_role_id") or "") in roles
    principal["can_view_abyss"] = can_view_abyss_tags(roles, config.get("abyss"))
    principal["can_create_work"] = True
    from datetime import datetime, timezone

    deadline = member.get("communication_disabled_until")
    try:
        muted = bool(deadline) and datetime.fromisoformat(
            deadline.replace("Z", "+00:00")
        ) > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(503, "成员禁言状态暂不可用")
    principal["can_speak"] = not muted
    principal["config"] = config
    principal["forums"] = dict(config.get("work_forums", {}))
    principal["forums"].setdefault("discord_guild_id", str(access.main_guild))
    guild_id = principal["forums"].get("guild_id")
    if not guild_id:
        raise HTTPException(503, "Fluxer 作品服务器尚未配置")
    guild_id = str(guild_id)
    state = request.scope.setdefault("state", {})
    cache_key = (principal["id"], guild_id)
    preferences = state.setdefault(_PREFERENCES_STATE_KEY, {})
    if cache_key not in preferences:
        preferences[cache_key] = await FluxerPreferencesClient().get_preferences(
            principal["id"], guild_id
        )
    snapshot = preferences[cache_key]
    # 展示偏好不等于年龄资格，任何来源的 NSFW 内容都同时满足两者。
    principal["show_nsfw"] = snapshot["show_nsfw"] and snapshot["nsfw_eligible"]
    principal["nsfw_preference_show_nsfw"] = snapshot["show_nsfw"]
    principal["nsfw_eligible"] = snapshot["nsfw_eligible"]
    principal["nsfw_preference_version"] = snapshot["version"]
    return principal


def get_session_factory():
    """单一可替换会话工厂供领域服务与隔离集成测试复用。"""
    from api.v2.routers import works

    return works.AsyncSessionFactory
