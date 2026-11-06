"""协议附件（Attachment）：封面、图库、版本文件、聊天、头像、讨论帖附件。

文件直传对象存储（R2），这里只存元数据、存储 key、分级结果和状态。
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, Index, text
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class Attachment(SQLModel, table=True):
    """一次上传对应一行；uploadId 等于附件 ID（10-25 第 7 条）。"""

    __tablename__ = "attachment"
    __table_args__ = (
        Index("ix_attachment_owner_created", "owner_id", "created_at"),
        Index(
            "ix_attachment_work_purpose",
            "work_id",
            "purpose",
            postgresql_where=text("work_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """附件编号，对外序列化为字符串。"""

    owner_id: str
    """上传者用户 ID。"""

    purpose: str
    """cover / gallery / version_file / chat / avatar / discussion。"""

    work_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """封面、图库、版本文件所属作品。"""

    channel_id: str | None = None
    """聊天附件所在频道或私信会话 ID。"""

    kind: str = "file"
    """image 或 file。"""

    filename: str
    """原始文件名（协议 Attachment.name）。"""

    mime: str
    """申请时声明的 MIME。"""

    size: int = Field(sa_column=Column(BigInteger, nullable=False))
    """申请时声明的字节数。"""

    storage_key: str = Field(unique=True)
    """对象存储里的 key。"""

    state: str = Field(default="uploading", index=True)
    """uploading（已申请未完成）/ processing / ready / rejected；uploading 不对外。"""

    rating: str | None = None
    """分级结果 general / r18 / r18g；分级出来之前为空。"""

    auto_rating: str | None = None
    """模型给的分级（含 suspect = 拿不准）。"""

    model_scores: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    """模型分数 {general, r18, r18g}。"""

    review_state: str = "auto"
    """auto / pending_review / reviewed。"""

    reject_reason: str | None = None
    """rating_conflict / blocked。"""

    width: int | None = None
    """图片宽。"""

    height: int | None = None
    """图片高。"""

    sha256: str | None = None
    """完成时计算的内容哈希（可选）。"""

    created_at: datetime = Field(default_factory=utc_now)
    """申请时间。"""

    completed_at: datetime | None = None
    """调用 complete 的时间。"""

    deleted_at: datetime | None = None
    """删除时间。"""
