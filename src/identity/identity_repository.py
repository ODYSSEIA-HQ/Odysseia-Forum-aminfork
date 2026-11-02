"""本地权威投影的批量访问，避免逐行 RPC。"""

from sqlalchemy import select
from models.discord_identity import DiscordIdentity
from models.user import User


class IdentityRepository:
    """未命中的绑定不会被猜测为另一个身份空间的 ID。"""

    def __init__(self, session):
        """复用调用方会话批量读取本地权威投影。"""
        self.session = session

    async def discord_to_fluxer(self, discord_ids):
        """集合读取 Discord 对应 Fluxer ID；缺失映射不出现在结果中。"""
        rows = await self.session.execute(
            select(DiscordIdentity).where(
                DiscordIdentity.discord_user_id.in_(
                    [str(value) for value in discord_ids]
                )
            )
        )
        return {row.discord_user_id: row.user_id for row in rows.scalars()}

    async def fluxer_to_discord(self, user_ids):
        """集合读取 Fluxer 对应 Discord ID，原生用户可没有绑定。"""
        rows = await self.session.execute(
            select(DiscordIdentity).where(
                DiscordIdentity.user_id.in_([str(value) for value in user_ids]),
                DiscordIdentity.is_current.is_(True),
            )
        )
        return {row.user_id: row.discord_user_id for row in rows.scalars()}

    async def profiles(self, user_ids):
        """一次读取统一用户资料，调用方负责处理缺失投影。"""
        rows = await self.session.execute(
            select(User).where(User.id.in_([str(value) for value in user_ids]))
        )
        return {row.id: row for row in rows.scalars()}
