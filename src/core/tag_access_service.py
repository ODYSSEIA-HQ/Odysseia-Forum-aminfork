import os

import httpx

from shared.tag_error import TagError
from identity.runtime import uses_canonical_storage


class TagAccessService:
    """使用服务端配置和 Discord 成员信息核验标签权限。"""

    def __init__(self, config):
        self.config = config
        self.main_guild = int(
            config.get("main_guild_id") or config.get("auth", {}).get("guild_id") or 0
        )
        self.cache = {}

    def bot_admin(self, user_id):
        """判断全局 BOT 管理员。"""
        return str(user_id) in {
            str(v) for v in self.config.get("bot_admin_user_ids", [])
        }

    async def get_member(self, user_id: int, guild_id: int) -> dict:
        """实时读取完整成员信息，供身份组判断和登录缓存回填使用。"""
        token = os.environ.get("BOT_TOKEN", "")
        if not token or not guild_id:
            raise TagError("permission_unavailable", "成员权限服务尚未就绪", 503)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}",
                headers={"Authorization": f"Bot {token}"},
            )
        if response.status_code == 404:
            raise TagError("forbidden", "用户不是服务器成员", 403)
        if response.status_code != 200:
            raise TagError("permission_unavailable", "暂时无法核验成员权限", 503)

        # 拒绝异常成功响应，避免将缺失的身份组误判为普通成员。
        member = response.json()
        if not isinstance(member, dict) or not isinstance(member.get("roles"), list):
            raise ValueError("Discord 成员身份组格式错误")
        if any(not isinstance(role, (str, int)) for role in member["roles"]):
            raise ValueError("Discord 成员身份组格式错误")
        return member

    async def roles(self, user_id, guild_id):
        """实时读取成员身份组并在单次业务请求内复用。"""
        key = (user_id, guild_id)
        if key in self.cache:
            return self.cache[key]

        # 权限检查使用实时成员数据，避免角色撤销后继续授权。
        member = await self.get_member(user_id, guild_id)
        self.cache[key] = {str(v) for v in member["roles"]}
        return self.cache[key]

    async def manager(self, user_id, guild_id):
        """核验指定服务器的管理组或全局 BOT 管理员。"""
        if self.bot_admin(user_id):
            return True
        role = str(self.config.get("management_role_id") or "")
        return bool(role) and role in await self.roles(user_id, guild_id)

    async def access(
        self, user_id, kind, target, manage=False, audit=False, canonical_user_id=None
    ):
        """核验目标可见性并返回是否具有直接修改权限。"""
        if self.bot_admin(user_id):
            return True
        guild_id = target.guild_id if kind == "thread" else self.main_guild
        main_roles = await self.roles(user_id, self.main_guild)
        roles = main_roles
        if guild_id != self.main_guild:
            try:
                roles = await self.roles(user_id, guild_id)
            except TagError as exc:
                if exc.status != 403:
                    raise
                roles = set()
        is_manager = str(self.config.get("management_role_id") or "") in roles
        owner = (
            (target.fluxer_author_id if kind == "thread" else target.fluxer_owner_id)
            if uses_canonical_storage()
            else (target.author_id if kind == "thread" else target.owner_id)
        )
        identity = canonical_user_id if uses_canonical_storage() else user_id
        if audit:
            if not is_manager:
                raise TagError("forbidden", "仅管理组可查看操作记录", 403)
            return True
        if is_manager:
            return True
        # 可访问用户沿用网站准入身份组和深渊区约束。
        auth = self.config.get("auth", {})
        required = auth.get("role_ids", [])
        required = required.split(",") if isinstance(required, str) else required
        if required and not main_roles.intersection(str(v).strip() for v in required):
            raise TagError("forbidden", "没有网站访问权限", 403)
        if kind == "thread":
            if not target.show_flag or getattr(target, "deleted_at", None):
                raise TagError("not_found", "帖子不可访问", 404)
            abyss = self.config.get("abyss", {})
            if target.channel_id in {int(v) for v in abyss.get("channel_ids", [])}:
                if str(abyss.get("required_role_id") or "") not in main_roles:
                    raise TagError("forbidden", "没有此频道的访问权限", 403)
        elif not target.is_public and owner != identity and not is_manager:
            raise TagError("not_found", "书单不可访问", 404)
        allowed = owner is not None and owner == identity or is_manager
        if manage and not allowed:
            raise TagError("forbidden", "仅作者或管理组可直接修改", 403)
        return allowed
