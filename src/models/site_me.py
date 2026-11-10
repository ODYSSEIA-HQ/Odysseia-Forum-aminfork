"""/api/v1「我的」、提醒、公告规则、作品内（确认/规则/钉选/DC 存档）、书单补充、好友用的表。

迁移：alembic/versions/site_v1_me_tables.py。能复用 V2 的事实都复用：
- 作品关注 thread_follow、作品公告 work_announcement、作品内禁言 work_user_mute；
- 书单 booklist / booklist_item / user_collection（这里只补协议多出来的 tag、封面附件、默认排序）；
- 提醒：V2 的 notification（公告、作者新作、标签审核）照读，站内新产生的提醒写 site_notification。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


# ---------------- 我的 ----------------


class SiteUserPreferencesDoc(SQLModel, table=True):
    """跟账号走的前端设置（GET/PUT /me/preferences），带版本号防覆盖。"""

    __tablename__ = "site_user_preferences_doc"

    user_id: str = Field(primary_key=True)
    """用户 ID。"""

    version: int = 0
    """每次保存加 1。"""

    data: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    """各分区原样保存。"""

    updated_at: datetime | None = None
    """最后保存时间。"""


class SiteTagHabits(SQLModel, table=True):
    """浏览习惯（GET/PUT/DELETE /me/tag-habits），整份替换。"""

    __tablename__ = "site_tag_habits"

    user_id: str = Field(primary_key=True)
    """用户 ID。"""

    entries: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """[{tagId, name, restricts, score, updatedAt}]。"""

    hidden: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """「不感兴趣」的 tag 名。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最后上传时间。"""


class SitePinnedWorks(SQLModel, table=True):
    """作者页精选（PUT /me/pinned-works）：[{workId, note}] 按顺序。"""

    __tablename__ = "site_pinned_works"

    user_id: str = Field(primary_key=True)
    """作者 ID。"""

    items: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """精选作品和一句话。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最后修改时间。"""


class SiteDecorationRole(SQLModel, table=True):
    """装饰身份组（勋章、名字颜色、头像框），活动发奖、站务发放。"""

    __tablename__ = "site_decoration_role"

    id: str = Field(primary_key=True)
    """身份组 ID。"""

    name: str
    """名字。"""

    description: str | None = None
    """说明。"""

    icon: str | None = None
    """勋章图地址。"""

    color: str | None = None
    """主色 #rrggbb。"""

    rarity: str = "common"
    """稀有度。"""

    slots: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """能提供的外观槽位（nameColor / frame / font / title）。"""

    sort: int = 0
    """排序，越大越优先（没自选的槽位取最高的）。"""


class SiteUserDecorationGrant(SQLModel, table=True):
    """谁拥有哪个装饰身份组。"""

    __tablename__ = "site_user_decoration_grant"

    user_id: str = Field(primary_key=True)
    """用户 ID。"""

    role_id: str = Field(primary_key=True)
    """装饰身份组 ID。"""

    granted_at: datetime = Field(default_factory=utc_now)
    """获得时间。"""

    source: str | None = None
    """获得来源。"""


class SiteUserDecorationPref(SQLModel, table=True):
    """勋章墙的自选：展示的勋章、名字旁不显示、各槽位用哪个组。"""

    __tablename__ = "site_user_decoration_pref"

    user_id: str = Field(primary_key=True)
    """用户 ID。"""

    showcase: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """展示中的勋章（身份组 ID）。"""

    showcase_hidden: bool = Field(
        default=False, sa_column_kwargs={"server_default": "false"}
    )
    """名字旁不显示勋章。"""

    equipped: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    """{槽位: 身份组 ID 或 null}。"""


class SitePluginToken(SQLModel, table=True):
    """酒馆插件令牌：只存哈希和末 4 位。"""

    __tablename__ = "site_plugin_token"

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """令牌编号。"""

    user_id: str = Field(index=True)
    """所属用户。"""

    name: str
    """用户起的名字。"""

    token_hash: str = Field(unique=True)
    """sha256(明文)。"""

    hint: str
    """明文末 4 位。"""

    scopes: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """权限。"""

    created_at: datetime = Field(default_factory=utc_now)
    """创建时间。"""

    last_used_at: datetime | None = None
    """插件最后一次使用时间。"""


# ---------------- 作品内 ----------------


class SiteWorkAck(SQLModel, table=True):
    """作品确认记录：每人每作品每类一行，存确认到的版本（公告序号 / 规则版本 / 版本 ID）。"""

    __tablename__ = "site_work_ack"

    user_id: str = Field(primary_key=True)
    """用户 ID。"""

    work_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """作品编号。"""

    kind: str = Field(primary_key=True)
    """announcement / rules / version。"""

    version: str
    """确认到的版本。"""

    acked_at: datetime = Field(default_factory=utc_now)
    """确认时间。"""


class SiteWorkRules(SQLModel, table=True):
    """作品交流区规则；没有行时按版本 1、空正文。"""

    __tablename__ = "site_work_rules"

    work_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """作品编号。"""

    body: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """规则正文（Markdown）。"""

    version: int = 1
    """每次保存加 1。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最后修改时间。"""


class SiteWorkPin(SQLModel, table=True):
    """作品钉选（聊天消息摘要）。"""

    __tablename__ = "site_work_pin"
    __table_args__ = (Index("ix_site_work_pin_work_created", "work_id", "created_at"),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """钉选编号。"""

    work_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    """作品编号。"""

    excerpt: str
    """摘要。"""

    author_id: str | None = None
    """原消息作者。"""

    message_id: str | None = None
    """原消息 ID。"""

    pinned_by: str
    """钉选的人。"""

    created_at: datetime = Field(default_factory=utc_now)
    """钉选时间。"""


class SiteDcArchivePost(SQLModel, table=True):
    """DC 原帖讨论存档（协议 3.19，只读）。数据由迁移任务导入，这里先只建表。"""

    __tablename__ = "site_dc_archive_post"
    __table_args__ = (
        Index("ix_site_dc_archive_post_work_created", "work_id", "created_at"),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """楼层编号。"""

    work_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    """作品编号。"""

    legacy_message_id: str
    """DC 原消息 ID。"""

    author_id: str | None = None
    """认领后的平台用户。"""

    legacy_author_name: str = ""
    """DC 上的作者名。"""

    body: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """正文。"""

    attachments: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """[{id, name, mime, size, url, rating}]（DC 迁移图）。"""

    reactions: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """[{emoji, count}]。"""

    reply_to: str | None = None
    """回复的楼层（legacyMessageId）。"""

    rating: str = "general"
    """楼层分级（图片最高分级）。"""

    hidden_state: str | None = None
    """by_author / by_manager / by_staff；没隐藏为空。"""

    created_at: datetime = Field(default_factory=utc_now)
    """原发帖时间。"""


# ---------------- 站务公告和规则中心 ----------------


class SiteAnnouncement(SQLModel, table=True):
    """站内公告：只存一份，确认按版本号比对。"""

    __tablename__ = "site_announcement"

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """公告编号。"""

    audience: str = "all"
    """all / r18。"""

    kind: str = "normal"
    """normal / banner / rule。"""

    title: str
    """标题。"""

    body: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """正文（Markdown）。"""

    requires_ack: bool = False
    """要不要强制确认。"""

    version: int = 1
    """内容版本，改了要重新确认时加 1。"""

    pinned: bool = False
    """公告板置顶。"""

    rule_slug: str | None = None
    """对应的规则中心文档。"""

    published_at: datetime = Field(default_factory=utc_now)
    """发布时间。"""

    deleted_at: datetime | None = None
    """撤下时间。"""


class SiteAnnouncementAck(SQLModel, table=True):
    """谁确认到了公告的哪个版本。"""

    __tablename__ = "site_announcement_ack"

    user_id: str = Field(primary_key=True)
    """用户 ID。"""

    announcement_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """公告编号。"""

    version: int
    """确认到的版本。"""

    acked_at: datetime = Field(default_factory=utc_now)
    """确认时间。"""


class SiteRuleDoc(SQLModel, table=True):
    """规则中心文档。"""

    __tablename__ = "site_rule_doc"

    slug: str = Field(primary_key=True)
    """文档标识。"""

    title: str
    """标题。"""

    summary: str = ""
    """摘要。"""

    body: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """正文（Markdown）。"""

    version: int = 1
    """版本。"""

    sort: int = 0
    """列表顺序（小的在前）。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最后修改时间。"""


# ---------------- 提醒 ----------------


class SiteNotification(SQLModel, table=True):
    """站内产生的提醒（好友、规则更新等）；分类按协议 Notification.category。"""

    __tablename__ = "site_notification"
    __table_args__ = (
        Index("ix_site_notification_user_created", "user_id", "created_at"),
        Index(
            "ix_site_notification_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """提醒编号。"""

    user_id: str
    """收件人。"""

    category: str
    """协议 NotificationCategory。"""

    actor_id: str | None = None
    """触发的人。"""

    target: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default="{}"),
    )
    """{type, id, title}。"""

    excerpt: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    """摘要。"""

    work_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """来源作品：按有效模式过滤（受限作品对全年龄模式不返回、不计数）。"""

    context: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """跳转用的上下文。"""

    mention: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """@ 的范围。"""

    created_at: datetime = Field(default_factory=utc_now)
    """创建时间。"""

    read_at: datetime | None = None
    """已读时间。"""


# ---------------- 书单补充 ----------------


class SiteCollectionMeta(SQLModel, table=True):
    """V2 书单（booklist）没有的协议字段：tag、上传的封面、默认排序 updated。"""

    __tablename__ = "site_collection_meta"

    booklist_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    """书单编号。"""

    tag_ids: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """site_tag ID 列表。"""

    cover_attachment_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """上传的封面附件。"""

    default_sort: str | None = None
    """order / added / updated；空时按 V2 default_sort_method 推。"""


# ---------------- 好友 ----------------


class SiteFriend(SQLModel, table=True):
    """好友关系：user_a < user_b 存一行。"""

    __tablename__ = "site_friend"

    user_a: str = Field(primary_key=True)
    """较小的用户 ID。"""

    user_b: str = Field(primary_key=True)
    """较大的用户 ID。"""

    since: datetime = Field(default_factory=utc_now)
    """成为好友的时间。"""


class SiteFriendRequest(SQLModel, table=True):
    """待处理的好友申请；接受、拒绝、撤回后删除。"""

    __tablename__ = "site_friend_request"
    __table_args__ = (
        UniqueConstraint("from_id", "to_id", name="uq_site_friend_request"),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """申请编号。"""

    from_id: str = Field(index=True)
    """申请人。"""

    to_id: str = Field(index=True)
    """被申请人。"""

    created_at: datetime = Field(default_factory=utc_now)
    """申请时间。"""
