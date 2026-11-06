"""Discord 正式成员身份组检查（真实环境）。

V2 每个请求都硬性要求正式成员身份组；/api/v1 只在需要时查（R18 资格的 discord_role 条件，
以及后面领域里需要「正式成员」的写操作），并由 SITE_REQUIRE_DISCORD_ROLE 开关控制。
复用 V2 的 WorkPermissionService（成员缓存、绑定版本、认证代次失效）。
"""

from fastapi import HTTPException
from sqlalchemy import select

from site_v1.context import Viewer
from site_v1.errors import unavailable

_service = None


def _required_roles() -> set[str]:
    from site_v1.config import get_config

    roles = set(get_config().discord_role_ids)
    if roles:
        return roles
    try:
        from api.v2.config import get_work_config

        raw = get_work_config().get("auth", {}).get("role_ids", [])
    except FileNotFoundError:
        raw = []
    raw = raw.split(",") if isinstance(raw, str) else raw
    return {str(r).strip() for r in raw if str(r).strip()}


async def has_member_role(viewer: Viewer) -> bool:
    """当前 Discord 绑定是否有正式成员身份组；Discord 不可用时抛 503。"""
    if not viewer.discord_user_id:
        return False
    required = _required_roles()
    if not required:
        return True
    global _service
    from api.v2.config import get_work_config
    from models.discord_identity import DiscordIdentity
    from site_v1.deps import get_session_factory

    async with get_session_factory()() as session:
        version = (
            await session.execute(
                select(DiscordIdentity.version).where(
                    DiscordIdentity.discord_user_id == viewer.discord_user_id,
                    DiscordIdentity.is_current.is_(True),
                )
            )
        ).scalar_one_or_none()
    if version is None:
        return False
    try:
        if _service is None:
            from work.work_permission_service import WorkPermissionService

            _service = WorkPermissionService(get_work_config())
        member = await _service.member(viewer.user_id, viewer.discord_user_id, version)
    except HTTPException as exc:
        if exc.status_code in (401, 403, 404):
            return False
        raise unavailable("Discord 身份组暂时无法核验") from exc
    roles = {str(r) for r in member.get("roles", [])}
    return bool(roles & required)
