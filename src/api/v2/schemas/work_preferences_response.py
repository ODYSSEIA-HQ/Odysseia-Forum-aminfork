"""V2 搜索偏好与 Fluxer 成人内容状态的响应协议。"""

from api.v1.schemas.preferences import UserPreferencesResponse


class WorkPreferencesResponse(UserPreferencesResponse):
    """完整旧搜索偏好与独立 Fluxer NSFW 权威状态。"""

    user_id: str
    """当前认证主体的 Fluxer 用户编号。"""

    guild_id: str
    """普通搜索偏好所属的 Discord 服务器编号。"""

    is_first_time: bool
    """当前搜索作用域是否尚未保存偏好。"""

    show_nsfw: bool
    """用户选择的成人内容显示开关，不代表年龄资格。"""

    nsfw_preference_version: int | None = None
    """Fluxer 成人内容偏好的权威版本。"""

    nsfw_guild_id: str
    """成人内容偏好所属的 Fluxer 服务器编号。"""

    nsfw_eligible: bool
    """用户是否满足原生年龄资格。"""
