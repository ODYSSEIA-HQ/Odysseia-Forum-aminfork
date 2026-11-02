"""跨路由与限频中间件共享同一认证服务。"""

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert
from identity.fluxer_auth_service import FluxerAuthService
from models.user import User
from shared.redis_client import RedisManager

_service = None


def get_auth_service():
    """按进程复用 NATS 客户端与并发上限。"""
    global _service
    if _service is None:
        _service = FluxerAuthService(RedisManager.get_client())
    return _service


async def resolve_principal(token, *, force_authority=False):
    """在统一资料投影中更新用户，Discord 权限只从明确绑定读取。"""
    identity = (
        await get_auth_service().resolve(token, force_authority=True)
        if force_authority
        else await get_auth_service().resolve(token)
    )
    if identity is None:
        return None
    from shared.database import AsyncSessionFactory

    try:
        async with AsyncSessionFactory() as session:
            values = {
                "id": identity["user_id"],
                "username": identity["username"],
                "display_name": identity.get("display_name"),
                "avatar_url": identity.get("avatar_url"),
            }
            statement = insert(User).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[User.id],
                    set_=values,
                    where=or_(
                        User.username.is_distinct_from(statement.excluded.username),
                        User.display_name.is_distinct_from(
                            statement.excluded.display_name
                        ),
                        User.avatar_url.is_distinct_from(statement.excluded.avatar_url),
                    ),
                )
            )
            discord_id = identity.get("discord_user_id")
            await session.commit()
    except Exception as exc:
        raise HTTPException(503, "用户资料投影暂不可用") from exc
    return {
        "id": identity["user_id"],
        "discord_user_id": discord_id,
        "username": identity["username"],
        "display_name": identity.get("display_name"),
        "global_name": identity.get("display_name"),
        "avatar_url": identity.get("avatar_url"),
        "avatar": None,
        "roles": [],
    }
