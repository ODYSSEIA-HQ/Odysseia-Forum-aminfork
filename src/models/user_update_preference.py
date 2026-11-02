from typing import Optional

from sqlalchemy import CheckConstraint
from sqlmodel import BigInteger, Column, Field, SQLModel, UniqueConstraint


class UserUpdatePreference(SQLModel, table=True):
    """用户更新检测偏好模型，存储自动同步/不再提醒状态"""

    __tablename__ = "user_update_preference"  # type: ignore
    __table_args__ = (
        UniqueConstraint("user_id", "thread_id", name="uk_user_thread_update_pref"),
        CheckConstraint(
            "NOT (auto_sync AND no_remind)",
            name="ck_user_update_preference_exclusive",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True, nullable=True),
        description="用户 Discord ID",
    )
    thread_id: int = Field(
        sa_column=Column(BigInteger, index=True, nullable=False),
        description="帖子 Discord Thread ID",
    )
    auto_sync: bool = Field(
        default=False,
        description="是否自动同步更新到索引页（无需再次确认）",
    )
    no_remind: bool = Field(
        default=False,
        description="是否不再提醒该帖子的更新检测",
    )

    fluxer_user_id: str | None = Field(
        default=None,
        index=True,
        description="此帖子更新检测偏好所属用户的 Fluxer ID",
    )
    """为此帖子保存自动同步或不再提醒偏好的用户 Fluxer ID。"""
