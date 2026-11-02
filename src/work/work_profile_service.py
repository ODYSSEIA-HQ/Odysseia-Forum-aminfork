"""账户偏好领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from work.work_user_repository import WorkUserRepository


class WorkProfileService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def me(self, command: WorkCommandQo):
        """公开前端需要的能力，不泄漏会话凭据。"""
        principal = command.actor
        return {
            key: principal[key]
            for key in (
                "id",
                "discord_user_id",
                "username",
                "display_name",
                "avatar_url",
                "is_admin",
                "can_create_work",
                "can_view_abyss",
                "can_speak",
                "show_nsfw",
            )
        }

    async def preferences(self, command: WorkCommandQo):
        """返回用户全局成人内容偏好。"""
        principal = command.actor
        return {"show_nsfw": principal["show_nsfw"]}

    async def save_preferences(self, command: WorkCommandQo):
        """偏好存统一用户表，不随服务器覆盖。"""
        principal = command.actor
        body = dict(command.values)
        if set(body) != {"show_nsfw"} or not isinstance(body["show_nsfw"], bool):
            raise HTTPException(422, "需要 show_nsfw 布尔值")
        async with self.sessions() as session:
            await WorkUserRepository(session).set_nsfw_preference(
                principal["id"], body["show_nsfw"]
            )
            await session.commit()
        return body
