"""协议 tag 目录（TagCatalog）：官方组、官方 tag、自由 tag、必答问题，以及作品上的 tag。

和 V1/V2 的 tag 表（7 类自定义标签 + DC 原生标签、带提议投票治理）是两套体系，
单独建表，免得新 tag 混进旧接口的标签列表、治理和触发器。旧自定义标签迁成自由 tag
是后续的数据任务（协议 7.3）。
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, Index, Text, text
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class SiteTagGroup(SQLModel, table=True):
    """官方 tag 组（rating/type/audience/form/theme/function/warning/attribute）。"""

    __tablename__ = "site_tag_group"

    id: str = Field(primary_key=True)
    """组 ID，取值见协议 TagGroup（free 不是组，不建行）。"""

    name: str
    """显示名，例如「分级」。"""

    required: bool = False
    """对所有资源类型都必选（分级、资源类型）。"""

    max_select: int = 1
    """这组最多选几个；1 表示单选（再选一个是替换）。"""

    only_for_types: list[str] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    """只有选了其中一个资源类型时这组才出现；为空表示所有类型都出现。"""

    required_for_types: list[str] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    """选了其中一个资源类型时这组必选。"""

    sort_order: int = 0
    """目录里的显示顺序。"""


class SiteTag(SQLModel, table=True):
    """官方 tag 和自由 tag。自由 tag 的 group_id 是 free。"""

    __tablename__ = "site_tag"
    __table_args__ = (
        Index(
            "uq_site_tag_group_name",
            "group_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """tag 内部编号，对外序列化为字符串。"""

    group_id: str = Field(index=True)
    """所属组 ID；自由 tag 为 free。"""

    name: str
    """tag 名。"""

    restricts: bool = False
    """是否限制可见（R18、R18G、破限等）。"""

    rating: str | None = None
    """只有分级组的 tag 有：general / r18 / r18g。"""

    official: bool = False
    """是否官方 tag（只有作者能加）。"""

    for_types: list[str] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    """只有选了这些资源类型才可选；为空表示不限。"""

    description: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """tag 说明（Markdown）。"""

    sort_order: int = 0
    """组内显示顺序。"""

    created_by: str | None = None
    """自由 tag 的创建者用户 ID；官方 tag 为空。"""

    created_at: datetime = Field(default_factory=utc_now)
    """创建时间。"""

    deleted_at: datetime | None = None
    """软删除时间。"""


class SiteTagQuestion(SQLModel, table=True):
    """发布时的必答问题，例如预设必答「是否含破限」。"""

    __tablename__ = "site_tag_question"

    id: str = Field(primary_key=True)
    """问题 ID，发布时 answers[].question 用它。"""

    text: str
    """问题。"""

    hint: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """问题说明（Markdown）。"""

    for_types: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """对哪些资源类型必答；为空表示所有类型。"""

    restricts_tag_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    """回答「含」时自动打上的 tag（site_tag.id）。"""

    sort_order: int = 0
    """显示顺序。"""


class SiteWorkTag(SQLModel, table=True):
    """作品上的协议 tag；移除后保留记录（removed_at）供 tag 日志使用。"""

    __tablename__ = "site_work_tag"
    __table_args__ = (
        Index(
            "uq_site_work_tag_active",
            "work_id",
            "tag_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        Index(
            "ix_site_work_tag_reverse",
            "tag_id",
            "work_id",
            postgresql_where=text("removed_at IS NULL"),
        ),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """绑定记录编号。"""

    work_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """作品编号。"""

    tag_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    """site_tag.id。"""

    added_by: str | None = None
    """加 tag 的用户 ID（协议 TagRef.addedBy）；系统自动加的为空。"""

    created_at: datetime = Field(default_factory=utc_now)
    """加上的时间。"""

    removed_at: datetime | None = None
    """移除时间；为空表示当前生效。"""

    removed_by: str | None = None
    """移除者用户 ID。"""
