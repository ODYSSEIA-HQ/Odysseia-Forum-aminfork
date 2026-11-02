from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Integer,
    SmallInteger,
)
from sqlmodel import Field, SQLModel

from shared.enum import TargetType
from shared.time_utils import utc_now


class BannerCarousel(SQLModel, table=True):
    """Banner轮播列表"""

    __tablename__ = "banner_carousel"  # type: ignore
    __table_args__ = (
        CheckConstraint(
            "(target_type=3 AND work_id IS NOT NULL AND thread_id IS NULL) OR "
            "(target_type IN (1,2) AND thread_id IS NOT NULL)",
            name="ck_banner_carousel_work_target",
        ),
        CheckConstraint(
            "target_type <> 2 OR cover_image_url IS NOT NULL",
            name="ck_banner_carousel_channel_cover",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            nullable=True,
        ),
        description="审核通过的申请ID；旧记录留空",
    )
    applicant_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True, nullable=True),
        description="申请人Discord ID；旧记录留空",
    )
    thread_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True, nullable=True),
        description="真实 Discord 帖子ID",
    )
    channel_id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, index=True),
        description="频道ID，NULL表示全频道",
    )
    cover_image_url: Optional[str] = Field(
        default=None,
        description="自定义封面图；帖子为空时动态使用首楼图",
    )
    title: str = Field(description="帖子标题")
    target_type: int = Field(
        default=TargetType.THREAD.value,
        sa_column=Column(SmallInteger, index=True, nullable=False),
        description="论坛帖子 / 频道",
    )

    start_time: datetime = Field(
        default_factory=utc_now,
        index=True,
        description="开始展示时间",
    )
    end_time: datetime = Field(index=True, description="结束展示时间（3天后）")
    position: int = Field(default=0, index=True, description="展示位置顺序")

    fluxer_applicant_id: str | None = Field(
        default=None,
        index=True,
        description="此 Banner 轮播项申请人的 Fluxer 用户 ID",
    )
    """当前轮播展示的 Banner 项目所对应申请人的 Fluxer 用户 ID。"""

    work_id: int | None = Field(default=None, sa_column=Column(BigInteger, index=True))
    """统一作品编号，原生作品不伪造 Discord 帖子编号。"""

    fluxer_owner_id: str | None = Field(default=None, index=True)
    """可合并的业务归属，与历史申请人和审核员审计分离。"""
