"""作品逐表情聚合。"""

from sqlalchemy import BigInteger, Column, CheckConstraint, JSON
from sqlmodel import SQLModel, Field


class WorkReaction(SQLModel, table=True):
    """按表情存汇总值，不存用户反应明细。"""

    __tablename__ = "work_reaction"
    __table_args__ = (CheckConstraint("count >= 0", name="ck_work_reaction_count"),)

    work_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """作品编号。"""

    emoji: str = Field(primary_key=True)
    """平台稳定表情键。"""

    emoji_data: dict = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    """保留原生 emoji ID、名称和动画标志。"""

    count: int = 0
    """汇总反应次数。"""
