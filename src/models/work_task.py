"""作品操作可靠任务。"""

from datetime import datetime
from sqlalchemy import BigInteger, Column, JSON
from sqlmodel import SQLModel, Field
from shared.time_utils import utc_now


class WorkTask(SQLModel, table=True):
    """持久化任务状态，Redis 只作为可丢失的调度提示。"""

    __tablename__ = "work_task"
    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """幂等操作编号。"""

    work_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """所属作品。"""

    kind: str
    """同步、移动或平台操作类型。"""

    dedupe_key: str = Field(unique=True)
    """防止重复事件创建任务。"""

    payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    """非敏感操作参数，不持久化会话 token。"""

    status: str = Field(default="pending", index=True)
    """pending、running、done、failed。"""

    attempts: int = 0
    """尝试次数。"""

    available_at: datetime = Field(default_factory=utc_now, index=True)
    """下一次可领取时间。"""

    dispatched_at: datetime | None = None
    """首次可能发送 RPC 的时间，未知结果必须以原操作重试。"""

    lease_until: datetime | None = None
    """崩溃后允许再次领取的租约截止。"""

    error: str | None = None
    """最近一次错误类别。"""
