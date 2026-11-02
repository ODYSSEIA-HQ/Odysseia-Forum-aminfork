"""原生平台标签到统一标签概念的来源映射。"""

from sqlalchemy import BigInteger, Column, UniqueConstraint, JSON
from sqlmodel import SQLModel, Field


class WorkNativeTagSource(SQLModel, table=True):
    """按平台、服务器、频道和原生标签编号区分来源。"""

    __tablename__ = "work_native_tag_source"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "guild_id",
            "channel_id",
            "external_id",
            name="uq_work_native_tag_identity",
        ),
    )
    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """来源内部编号。"""

    platform: str
    """原生平台。"""

    guild_id: str
    """原生服务器。"""

    channel_id: str
    """原生父论坛。"""

    external_id: str
    """平台标签编号。"""

    tag_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """统一标签概念编号。"""

    name: str
    """原生标签名称快照。"""

    emoji: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    """原生标签表情。"""
