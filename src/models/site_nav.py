"""/api/v1 找作品一块（nav）的新表：tag 同义词、禁用记录、公共聊天区频道、横幅申请的协议字段。

首页栏位、精选和推广位配置是站点级配置，存在 site_setting（home.rails、home.featured），不单独建表。
作品 tag 的增删记录直接用 site_work_tag（移除保留记录），不再单独记日志。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Column, Index, Text
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class SiteTagAlias(SQLModel, table=True):
    """tag 同义词：输入这些词会归到 tag_id（搜索、联想、打 tag、按名字打开 tag 页）。"""

    __tablename__ = "site_tag_alias"
    __table_args__ = (Index("uq_site_tag_alias_name", "name_lower", unique=True),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """内部编号。"""

    tag_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """主 tag（site_tag.id）。"""

    name: str
    """同义词原样（显示用）。"""

    name_lower: str
    """小写，唯一。"""

    created_by: str | None = None
    """设置者用户 ID；合并、改名自动留下的为执行合并的站务。"""

    created_at: datetime = Field(default_factory=utc_now)
    """设置时间。"""


class SiteTagDisabled(SQLModel, table=True):
    """被禁用的 tag 名：从所有作品上移除，之后不能再建同名 tag（官方、自由都不行）。"""

    __tablename__ = "site_tag_disabled"
    __table_args__ = (Index("uq_site_tag_disabled_name", "name_lower", unique=True),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """内部编号。"""

    tag_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    """被禁用的 site_tag.id（那一行同时软删除）。"""

    name: str
    """禁用时的名字。"""

    name_lower: str
    """小写，唯一。"""

    reason: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """禁用理由。"""

    disabled_by: str | None = None
    """操作的站务用户 ID。"""

    disabled_at: datetime = Field(default_factory=utc_now)
    """禁用时间。"""


class SitePublicChannel(SQLModel, table=True):
    """公共聊天区的频道（站务建、改、排序、删）。消息本身由聊天一块负责。

    fluxer_channel_id：对应的 Fluxer 频道 ID（聊天消息实际所在），可空——还没在 Fluxer 建好时为空，
    由聊天一块或运维回填；频道对象本身的增删改不依赖它。
    """

    __tablename__ = "site_public_channel"

    id: str = Field(primary_key=True)
    """频道 ID：2-32 位小写字母、数字、连字符，站务可指定，不填由服务器生成。"""

    name: str
    """频道名（1-20 字）。"""

    description: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """简介（最多 200 字）。"""

    rating: str = "general"
    """频道分级：general / r18 / r18g；全年龄模式下非全年龄频道不返回、按 ID 打开 404。"""

    sort_order: int = 0
    """侧栏顺序（协议 order），0..n-1，小的在前。"""

    rules: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """置顶的频道规则（Markdown，最多 5000 字）。"""

    fluxer_channel_id: str | None = Field(default=None, index=True)
    """对应的 Fluxer 频道 ID，可空。"""

    created_by: str | None = None
    """建频道的站务用户 ID。"""

    created_at: datetime = Field(default_factory=utc_now)
    """创建时间。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最近修改时间。"""


class SiteBannerMeta(SQLModel, table=True):
    """横幅申请（banner_application，和 V2 同一份事实）上 /api/v1 多出的字段。

    V2 的申请只有封面 URL 和固定 3 天的轮播；协议的申请带上传的横幅图（附件）、备注，
    审核通过时由审核员给出展示期。全站横幅通过时同时写 banner_carousel（V2 首页也能看到），
    tag 主榜横幅只在这里记展示期。
    """

    __tablename__ = "site_banner_meta"

    application_id: int = Field(primary_key=True)
    """banner_application.id。"""

    cover_attachment_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """另传的横幅图（attachment.id）；为空时用作品封面。"""

    note: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """申请备注（最多 500 字）。"""

    start_at: datetime | None = None
    """展示开始（审核通过时填）。"""

    end_at: datetime | None = None
    """展示结束。"""

    carousel_id: int | None = None
    """全站横幅通过后写进的 banner_carousel.id。"""
