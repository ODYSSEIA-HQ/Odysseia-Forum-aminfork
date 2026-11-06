"""每个 Forum 社区显式开关同步的单调版本，不初始化或迁移历史开关。"""

from sqlalchemy import BigInteger, Column
from sqlmodel import Field, SQLModel


class ForumNsfwSyncState(SQLModel, table=True):
    """与 User.show_nsfw 在同一事务提交的重复/乱序保护。"""

    __tablename__ = "forum_nsfw_sync_state"
    user_id: str = Field(primary_key=True)
    """统一用户编号。"""

    guild_id: str = Field(primary_key=True)
    """允许同步的原生社区编号。"""

    nsfw_version: int = Field(sa_column=Column(BigInteger, nullable=False))
    """已确认应用的单调版本。"""

    show_nsfw: bool
    """该版本对应的本地展示开关。"""
