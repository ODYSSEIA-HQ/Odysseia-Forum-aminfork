"""权威绑定的只读本地投影。"""

from sqlalchemy import Index, text
from sqlmodel import SQLModel, Field


class DiscordIdentity(SQLModel, table=True):
    """只从 Fluxer 导入 Discord 绑定，不分配账户 ID。"""

    __tablename__ = "discord_identity"

    __table_args__ = (
        Index(
            "uq_discord_identity_current_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    is_current: bool = Field(default=True, index=True)
    """历史归属永久保留，只有当前绑定可用于登录和权限。"""

    discord_user_id: str = Field(primary_key=True)
    """权威绑定中的 Discord 用户 ID，保留字符串形式。"""

    user_id: str = Field(index=True)
    """该 Discord 账户绑定的 Fluxer 权威用户 ID。"""

    version: int = 1
    """Fluxer 权威绑定版本，用于拒绝旧事件覆盖。"""
