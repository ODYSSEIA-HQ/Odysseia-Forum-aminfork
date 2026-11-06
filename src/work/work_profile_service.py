"""账户偏好领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from work.work_preferences_service import WorkPreferencesService


class WorkProfileService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def me(self, command: WorkCommandQo):
        """公开前端需要的能力，不泄漏会话凭据。"""
        principal = command.actor
        result = {
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
                "nsfw_preference_version",
                "nsfw_eligible",
            )
        }
        if principal.get("nsfw_preference_show_nsfw") is not None:
            result["show_nsfw"] = principal["nsfw_preference_show_nsfw"]
        return result

    async def preferences(self, command: WorkCommandQo):
        """返回同一身份及主服务器下的完整搜索偏好。"""
        return await WorkPreferencesService(self.sessions).preferences(command.actor)

    async def save_preferences(self, command: WorkCommandQo):
        """普通偏好部分更新；成人内容独立双写 Forum 与 Fluxer CAS。"""
        return await WorkPreferencesService(self.sessions).save_preferences(
            command.actor, command.values
        )
