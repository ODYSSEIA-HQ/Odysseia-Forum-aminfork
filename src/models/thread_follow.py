from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Index, UniqueConstraint, text
from sqlmodel import Column, Field, SQLModel

from shared.time_utils import utc_now


class ThreadFollow(SQLModel, table=True):
    """用户关注帖子的关联表"""

    __tablename__ = "thread_follow"  # type: ignore
    __table_args__ = (
        Index(
            "ix_thread_follow_active_work_time",
            "work_id",
            "followed_at",
            postgresql_where=text("active_flag IS TRUE"),
        ),
        UniqueConstraint("user_id", "thread_id", name="uk_thread_follow_user_thread"),
        UniqueConstraint(
            "fluxer_user_id", "work_id", name="uq_thread_follow_user_work"
        ),
        Index(
            "ix_thread_follow_thread_active_user",
            "thread_id",
            "active_flag",
            "user_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True),
        description="用户Discord ID",
    )
    work_id: int | None = Field(default=None, sa_column=Column(BigInteger, index=True))
    """统一作品关注编号，沿用旧关注记录及已读状态。"""

    thread_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True),
        description="帖子Discord ID",
    )
    followed_at: datetime = Field(default_factory=utc_now, description="关注时间")
    last_viewed_at: Optional[datetime] = Field(
        default=None, description="最后查看时间，用于计算未读更新"
    )
    active_flag: bool = Field(
        default=True, description="是否为当前关注（False表示过去关注）"
    )

    fluxer_user_id: str | None = Field(
        default=None,
        index=True,
        description="关注此帖子的用户 Fluxer ID",
    )
    """发起此帖子关注关系的用户 Fluxer ID，用于记录其关注和最后查看状态。"""
