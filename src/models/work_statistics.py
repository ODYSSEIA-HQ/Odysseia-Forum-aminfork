"""作品的持久统计。"""

from sqlalchemy import BigInteger, Column, CheckConstraint
from sqlmodel import SQLModel, Field


class WorkStatistics(SQLModel, table=True):
    """保存可重建及累加统计，不保存浏览去重记录。"""

    __tablename__ = "work_statistics"
    __table_args__ = (
        CheckConstraint(
            "view_count >= 0 AND collection_count >= 0 AND reaction_count >= 0 AND reply_count >= 0",
            name="ck_work_statistics_nonnegative",
        ),
    )

    work_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """作品内部编号。"""

    display_count: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    """展示计数用于既有综合排序，不与浏览去重计数混用。"""

    view_count: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    """有效浏览次数。"""

    collection_count: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False)
    )
    """默认书单收藏人数。"""

    reaction_count: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    """平台反应汇总。"""

    reply_count: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    """平台回复汇总。"""

    download_count: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False, server_default="0")
    )
    """协议 /api/v1 的版本文件下载次数（Work.stats.downloads）。"""
