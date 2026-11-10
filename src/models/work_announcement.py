"""作品作者公告。"""

from datetime import datetime
from sqlalchemy import BigInteger, Column, JSON, Index, text
from sqlmodel import SQLModel, Field
from shared.time_utils import utc_now


class WorkAnnouncement(SQLModel, table=True):
    """每次发布独立通知，编辑不会生成新通知。"""

    __tablename__ = "work_announcement"
    __table_args__ = (
        Index(
            "ix_work_announcement_work_created_id",
            "work_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )
    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """公告编号。"""

    legacy_update_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, unique=True)
    )
    """历史 ThreadUpdate 的来源编号，迁移不补发通知。"""

    work_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """作品编号。"""

    author_id: str | None = None
    """发布作者。"""

    title: str | None = None
    """可选标题。"""

    body: str
    """公告正文。"""

    images: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    """公告图片。"""

    created_at: datetime = Field(default_factory=utc_now)
    """发布时间。"""

    withdrawn_at: datetime | None = None
    """撤回时间，历史保留。"""
