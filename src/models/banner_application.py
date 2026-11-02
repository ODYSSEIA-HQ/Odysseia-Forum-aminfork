from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, Column, SmallInteger
from sqlmodel import Field, SQLModel

from shared.enum import ApplicationStatus, TargetType
from shared.time_utils import utc_now


class BannerApplication(SQLModel, table=True):
    """Banner申请记录"""

    __tablename__ = "banner_application"  # type: ignore
    __table_args__ = (
        CheckConstraint(
            "(target_type=3 AND work_id IS NOT NULL AND thread_id IS NULL) OR "
            "(target_type IN (1,2) AND thread_id IS NOT NULL)",
            name="ck_banner_application_work_target",
        ),
        CheckConstraint(
            "target_type <> 2 OR cover_image_url IS NOT NULL",
            name="ck_banner_application_channel_cover",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    thread_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True, nullable=True),
        description="真实 Discord 帖子ID",
    )
    channel_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True),
        description="帖子所在频道ID",
    )
    applicant_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, index=True),
        description="申请人Discord ID",
    )
    cover_image_url: Optional[str] = Field(
        default=None,
        description="自定义封面图；帖子为空时动态使用首楼图",
    )
    target_scope: str = Field(
        index=True, description="目标范围：'global'表示全频道，或具体频道ID"
    )
    target_type: int = Field(
        default=TargetType.THREAD.value,
        sa_column=Column(SmallInteger, index=True, nullable=False),
        description="论坛帖子 / 频道",
    )

    # 审核相关
    status: str = Field(
        default=ApplicationStatus.PENDING.value, index=True, description="申请状态"
    )
    applied_at: datetime = Field(default_factory=utc_now, description="申请时间")
    reviewed_at: Optional[datetime] = Field(default=None, description="审核时间")
    reviewer_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger),
        description="审核员Discord ID",
    )
    reject_reason: Optional[str] = Field(default=None, description="拒绝理由")

    # 审核记录消息ID（用于在指定thread中发送审核记录）
    review_message_id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger),
        description="审核记录消息ID",
    )
    review_thread_id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger),
        description="审核记录所在的thread ID",
    )

    fluxer_applicant_id: str | None = Field(
        default=None,
        index=True,
        description="Banner 展示申请人的 Fluxer 用户 ID",
    )
    """提交此 Banner 展示申请的用户 Fluxer ID。"""

    fluxer_reviewer_id: str | None = Field(
        default=None,
        index=True,
        description="Banner 申请审核员的 Fluxer 用户 ID",
    )
    """审核此 Banner 展示申请的用户 Fluxer ID，与提交申请的用户区分。"""

    work_id: int | None = Field(default=None, sa_column=Column(BigInteger, index=True))
    """统一作品编号，原生作品不伪造 Discord 帖子编号。"""

    fluxer_owner_id: str | None = Field(default=None, index=True)
    """可合并的业务归属，与历史申请人和审核员审计分离。"""
