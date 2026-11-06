"""受 NATS ACL 保护的单向应用边界；不向 Fluxer 再发送偏好写入。"""

import re

from fastapi import HTTPException
from sqlalchemy import select, text

from models.forum_nsfw_sync_state import ForumNsfwSyncState
from models.user import User


class ForumNsfwSyncReceiver:
    """只同步配置社区的显式 CAS 写入，独立保存 Forum 开关。"""

    def __init__(self, sessions, guild_id):
        """保存请求内使用的会话工厂与服务配置。"""
        self.sessions = sessions
        self.guild_id = str(guild_id or "")

    async def apply(self, payload):
        """校验同步范围并在同一事务内应用单调版本与本地开关。"""
        # 在打开事务前验证协议版本、身份格式与社区范围。
        keys = {
            "version",
            "request_id",
            "user_id",
            "guild_id",
            "show_nsfw",
            "nsfw_version",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != keys
            or type(payload.get("version")) is not int
            or payload["version"] != 1
            or not isinstance(payload.get("request_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["request_id"])
            or type(payload.get("show_nsfw")) is not bool
            or type(payload.get("nsfw_version")) is not int
            or not 1 <= payload["nsfw_version"] <= 2**53 - 1
        ):
            raise HTTPException(422, "Invalid synchronization envelope")
        for key in ("user_id", "guild_id"):
            value = payload[key]
            if (
                not isinstance(value, str)
                or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
                or int(value) > 2**63 - 1
            ):
                raise HTTPException(422, "Invalid synchronization identity")
        if not self.guild_id or payload["guild_id"] != self.guild_id:
            raise HTTPException(403, "Synchronization scope unavailable")
        async with self.sessions() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            user = (
                await session.execute(
                    select(User).where(User.id == payload["user_id"]).with_for_update()
                )
            ).scalar_one_or_none()
            if user is None or user.merged_into_id:
                raise HTTPException(403, "Synchronization account unavailable")
            state = await session.get(
                ForumNsfwSyncState, (payload["user_id"], self.guild_id)
            )
            version = payload["nsfw_version"]
            if (
                state is not None
                and state.nsfw_version == version
                and state.show_nsfw != payload["show_nsfw"]
            ):
                raise HTTPException(503, "Synchronization version conflict")
            if state is None:
                state = ForumNsfwSyncState(
                    user_id=user.id,
                    guild_id=self.guild_id,
                    nsfw_version=version,
                    show_nsfw=payload["show_nsfw"],
                )
                session.add(state)
                user.show_nsfw = state.show_nsfw
            elif version > state.nsfw_version:
                state.nsfw_version = version
                state.show_nsfw = payload["show_nsfw"]
                user.show_nsfw = state.show_nsfw
            elif version == state.nsfw_version:
                # 幂等重试确认同一操作；不要让旧入口的无版本写入伪造同步确认。
                user.show_nsfw = state.show_nsfw
            result = {
                "user_id": user.id,
                "guild_id": self.guild_id,
                "show_nsfw": state.show_nsfw,
                "nsfw_version": state.nsfw_version,
            }
            await session.commit()
            return result
