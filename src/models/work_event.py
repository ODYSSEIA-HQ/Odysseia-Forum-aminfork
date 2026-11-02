"""跨平台内容事件的永久幂等收件箱。"""

from datetime import datetime
from sqlalchemy import JSON, Column
from sqlmodel import SQLModel, Field
from shared.time_utils import utc_now


class WorkEvent(SQLModel, table=True):
    """事件编号永久保留，不能依赖 Redis TTL 避免重复。"""

    __tablename__ = "work_event"
    event_id: str = Field(primary_key=True)
    """平台事件幂等编号。"""

    payload: dict = Field(sa_column=Column(JSON, nullable=False))
    """完整非敏感快照，用于审计与重放。"""

    received_at: datetime = Field(default_factory=utc_now)
    """收件时间。"""
