"""作品范围禁言的独立来源。"""

from datetime import datetime
from sqlalchemy import BigInteger, Column, Index, text
from sqlmodel import SQLModel, Field
from shared.time_utils import utc_now


class WorkUserMute(SQLModel, table=True):
    """只限制原生发言及创建子帖，标签收藏与反应不受影响。"""

    __tablename__ = "work_user_mute"
    __table_args__ = (
        Index(
            "uq_active_work_mute_source",
            "work_id",
            "user_id",
            "actor_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """处罚编号。"""

    work_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """作品编号。"""

    user_id: str = Field(index=True)
    """被限制的统一用户。"""

    actor_id: str
    """独立处罚来源操作者。"""

    reason: str
    """处罚原因。"""

    created_at: datetime = Field(default_factory=utc_now)
    """创建时间。"""

    expires_at: datetime | None = Field(default=None, index=True)
    """到期时间，空为永久。"""

    revoked_at: datetime | None = None
    """撤销或到期处理时间。"""
