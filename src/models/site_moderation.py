"""协议举报审核（3.10）：举报、审核队列项、用户处罚记录。

作品内禁言沿用 V2 的 work_user_mute；这里的 mute 是全站禁言。
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, Index, Text, text
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class Report(SQLModel, table=True):
    """一条举报。同一账号对同一待处理对象重复举报返回 409（由部分唯一索引保证）。"""

    __tablename__ = "report"
    __table_args__ = (
        Index(
            "uq_report_pending_reporter_target",
            "reporter_id",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_report_queue_item", "queue_item_id"),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """举报编号。"""

    reporter_id: str
    """举报人用户 ID。"""

    target_type: str
    """协议 ReportTargetType。"""

    target_id: str
    """被举报对象 ID（字符串，消息、帖子在 Fluxer 里）。"""

    reason: str
    """协议 ReportReason。"""

    note: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """补充说明，最多 500 字。"""

    state: str = Field(
        default="pending", sa_column_kwargs={"server_default": "pending"}
    )
    """pending / resolved。"""

    queue_item_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """聚合到的审核队列项。"""

    created_at: datetime = Field(default_factory=utc_now)
    """举报时间。"""


class ModerationQueueItem(SQLModel, table=True):
    """审核队列项：type=image（图片分级）或 report（举报聚合）。"""

    __tablename__ = "moderation_queue_item"
    __table_args__ = (
        Index(
            "uq_moderation_queue_pending_target",
            "type",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_moderation_queue_state_created", "state", "type", "created_at"),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """队列项编号。"""

    type: str
    """image / report。"""

    state: str = Field(
        default="pending", sa_column_kwargs={"server_default": "pending"}
    )
    """pending / resolved。"""

    source: str = "report"
    """model / report / crowd / static_check / appeal。"""

    target_type: str
    """被审对象类型（image 时为 upload）。"""

    target_id: str
    """被审对象 ID。"""

    attachment_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """图片队列对应的附件。"""

    work_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """相关作品。"""

    subject_user_id: str | None = Field(default=None, index=True)
    """被审内容的作者 / 上传者。"""

    auto_rating: str | None = None
    """图片队列：模型分级（含 suspect）。"""

    model_scores: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    """图片队列：模型分数。"""

    usage: str | None = None
    """图片队列：cover / gallery / chat / discussion。"""

    appealed: bool = False
    """是否申诉过。"""

    appeal_note: str | None = None
    """申诉说明。"""

    target_snapshot: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    """举报对象快照（对象被删后仍能看），协议 ReportQueueItem.target。"""

    resolution: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """处理结果：{decision, rating, addTags, removeTags, penalty, reason, by, at}。"""

    resolved_by: str | None = None
    """处理人用户 ID。"""

    resolved_at: datetime | None = None
    """处理时间。"""

    created_at: datetime = Field(default_factory=utc_now)
    """入队时间。"""


class UserModerationRecord(SQLModel, table=True):
    """用户处罚和内容调整记录：warning / mute（处罚）、adjustment（调整作品）。"""

    __tablename__ = "user_moderation_record"
    __table_args__ = (
        Index("ix_user_moderation_record_user_created", "user_id", "created_at"),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """记录编号。"""

    user_id: str
    """被处理的用户 ID。"""

    kind: str
    """warning / mute / adjustment。"""

    until: datetime | None = None
    """禁言截止时间（mute）。"""

    reason: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """原因。"""

    strike: bool = False
    """是否计入违规次数（strikes90d）。"""

    work_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """adjustment：相关作品。"""

    work_title: str | None = None
    """adjustment：当时的作品标题。"""

    summary: str | None = None
    """adjustment：调整摘要。"""

    by_user_id: str
    """处理人用户 ID。"""

    queue_item_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """来源审核队列项。"""

    created_at: datetime = Field(default_factory=utc_now)
    """记录时间。"""
