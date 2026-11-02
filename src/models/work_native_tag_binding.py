"""作品的原生来源标签绑定。"""

from sqlalchemy import BigInteger, Column
from sqlmodel import SQLModel, Field


class WorkNativeTagBinding(SQLModel, table=True):
    """与本地 TagBinding 分开保存，原生解绑不会删除本地轮次。"""

    __tablename__ = "work_native_tag_binding"
    work_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """作品编号。"""

    source_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """原生来源编号。"""
