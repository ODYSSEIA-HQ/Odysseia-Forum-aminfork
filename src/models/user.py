"""Fluxer 用户资料在 Forum 的本地投影。"""

from datetime import datetime
from sqlmodel import SQLModel, Field
from shared.time_utils import utc_now


class User(SQLModel, table=True):
    """统一账户与作者；id 始终是 Fluxer 权威 ID。"""

    __tablename__ = "user"

    id: str = Field(primary_key=True)
    """Fluxer 权威用户 ID，同时作为统一作者身份。"""

    username: str
    """Fluxer 原生唯一用户名。"""

    display_name: str | None = None
    """Fluxer 原生显示名称，可为空。"""

    avatar_url: str | None = None
    """Fluxer 原生头像地址，可为空。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """本地用户资料投影的更新时间。"""

    show_nsfw: bool = False
    """全局成人内容显示偏好。"""

    merged_into_id: str | None = None
    """管理合并后的最终账户，保留旧用户以便审计。"""
