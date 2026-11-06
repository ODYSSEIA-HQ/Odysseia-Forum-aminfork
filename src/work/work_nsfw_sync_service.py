"""两种界面共用 Fluxer CAS，再经受信回调提交 Forum，避免双向死锁。"""

import asyncio
from fastapi import HTTPException
from identity.fluxer_preferences_client import FluxerPreferencesClient
from models.forum_nsfw_sync_state import ForumNsfwSyncState
from models.user import User


class WorkNsfwSyncService:
    """远端持久意图负责恢复；本地读取仍独立使用 User.show_nsfw。"""

    def __init__(self, sessions):
        """保存请求内使用的会话工厂与服务配置。"""
        self.sessions = sessions

    async def save(self, user_id, guild_id, show_nsfw, expected_version):
        """通过远端版本写入并核对本地回执，拒绝未确认的同步结果。"""
        # 远端调用与本地事务分开，避免反向同步等待同一把行锁。
        try:
            # 不跨 RPC 持有本地事务/行锁：反向 RPC 需要在另一连接写入同一行。
            async with self.sessions() as session:
                user = await session.get(User, user_id)
                if user is None or user.merged_into_id:
                    raise HTTPException(403, "Forum 账户不可用，请重新登录")
            async with asyncio.timeout(25):
                result = await FluxerPreferencesClient().update_preferences(
                    user_id, guild_id, show_nsfw, expected_version
                )
            # 防止只升级 Forum 或旧 Fluxer 未回调时误报两端成功。
            async with self.sessions() as session:
                user = await session.get(User, user_id)
                state = await session.get(ForumNsfwSyncState, (user_id, guild_id))
                if (
                    user is None
                    or user.merged_into_id
                    or state is None
                    or state.nsfw_version != result["version"]
                    or state.show_nsfw is not show_nsfw
                    or user.show_nsfw is not show_nsfw
                ):
                    raise HTTPException(503, "Unconfirmed Forum synchronization")
            return result
        except HTTPException as exc:
            if exc.status_code in (403, 409, 422):
                raise
            raise HTTPException(
                503,
                "两端成人内容开关尚未确认同步，后台将恢复；请刷新后核对，勿假定已回滚",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                503,
                "两端成人内容开关尚未确认同步，后台将恢复；请刷新后核对，勿假定已回滚",
            ) from exc
