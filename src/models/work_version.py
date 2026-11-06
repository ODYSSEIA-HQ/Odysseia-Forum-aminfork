"""协议作品版本（Version）及其文件。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class WorkVersion(SQLModel, table=True):
    """作品的一个版本：名字、更新说明、授权、依赖；文件在 work_version_file。"""

    __tablename__ = "work_version"
    __table_args__ = (UniqueConstraint("work_id", "name", name="uq_work_version_name"),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """版本编号，对外序列化为字符串。"""

    work_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """作品编号。"""

    name: str
    """版本名（1-40 字，同一作品内不重复）。"""

    changelog: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """更新说明（Markdown）。"""

    license: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """授权说明。"""

    dependencies: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """依赖列表：[{workId|null, url|null, name}]。"""

    created_by: str | None = None
    """发版本的用户 ID。"""

    created_at: datetime = Field(default_factory=utc_now, index=True)
    """创建时间；列表按它倒序。"""


class WorkVersionFile(SQLModel, table=True):
    """版本和附件的关联（附件 purpose=version_file）。"""

    __tablename__ = "work_version_file"

    version_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """work_version.id。"""

    attachment_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """attachment.id。"""

    position: int = 0
    """文件在版本里的顺序。"""
