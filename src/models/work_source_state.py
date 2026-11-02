"""平台来源的持久版本高水位和真实删除墓碑。"""

from sqlalchemy import BigInteger, Column
from sqlmodel import SQLModel, Field


class WorkSourceState(SQLModel, table=True):
    """删除先于创建事件到达时也禁止旧快照复活内容。"""

    __tablename__ = "work_source_state"
    platform: str = Field(primary_key=True)
    """来源平台。"""

    thread_id: str = Field(primary_key=True)
    """平台内来源帖子编号。"""

    version: int = Field(sa_column=Column(BigInteger, nullable=False))
    """已接收的事件版本，与RPC版本隔离。"""

    deleted: bool = False
    """权威删除墓碑，不由普通changed事件清除。"""
