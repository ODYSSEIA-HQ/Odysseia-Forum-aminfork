"""原生来源作者资料的权威补全。"""

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from models.user import User


class IdentityProfileService:
    """仅缺失资料时调用权威批量RPC，不猜用户名或复制平台编号。"""

    def __init__(self, session, rpc):
        """复用事件事务与已连接的服务身份RPC。"""
        self.session = session
        self.rpc = rpc

    async def ensure(self, source_ids):
        """先沿管理合并链找到业务主体，再一次补全缺失的用户资料。"""
        rows = await self.session.execute(
            text(
                "SELECT value,canonical_forum_user(value) FROM unnest(CAST(:ids AS VARCHAR[])) value"
            ),
            {"ids": sorted({str(value) for value in source_ids})},
        )
        ids = {canonical or original for original, canonical in rows}
        existing = set(
            (
                await self.session.execute(select(User.id).where(User.id.in_(ids)))
            ).scalars()
        )
        missing = ids - existing
        if not missing:
            return
        result = await self.rpc("rpc.identity.v1.profiles", user_ids=sorted(missing))
        profiles = result.get("profiles", [])
        accepted = {
            str(profile.get("user_id")): profile
            for profile in profiles
            if isinstance(profile.get("username"), str) and profile["username"]
        }
        if not missing.issubset(accepted):
            raise ValueError("来源作者权威资料尚未可用")
        await self.session.execute(
            insert(User)
            .values(
                [
                    {
                        "id": identifier,
                        "username": accepted[identifier]["username"],
                        "display_name": accepted[identifier].get("display_name"),
                        "avatar_url": accepted[identifier].get("avatar_url"),
                    }
                    for identifier in sorted(missing)
                ]
            )
            .on_conflict_do_nothing(index_elements=[User.id])
        )
