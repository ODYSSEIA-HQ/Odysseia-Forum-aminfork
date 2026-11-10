"""协议 /api/v1 层的账号侧本地状态：资料补充、偏好、R18 资格、屏蔽、假 Fluxer 凭证。

身份仍以 Fluxer 为权威（user.id 是 Fluxer 用户 ID），这里只存协议要求、
Fluxer 不管的站内状态。关注用户复用既有 author_follow，不另建表。
"""

from datetime import datetime

from sqlalchemy import JSON, Column, Index, text
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class SiteAccount(SQLModel, table=True):
    """站内账号补充资料；第一次经 /api/v1 访问时懒建。"""

    __tablename__ = "site_account"

    user_id: str = Field(primary_key=True)
    """Fluxer 权威用户 ID，对应 user.id。"""

    email: str | None = None
    """账号邮箱（只给本人看）；Fluxer 侧有权威邮箱时以 Fluxer 为准。"""

    created_at: datetime = Field(default_factory=utc_now)
    """站内账号建立时间，作为协议 User.createdAt。"""

    newcomer_until: datetime | None = None
    """新人缓冲期截止时间；为空或已过期表示不是新人。"""

    quiz_passed: bool = Field(
        default=False, sa_column_kwargs={"server_default": "false"}
    )
    """是否通过新人答题（答题是第二期，第一期只读）。"""

    permissions: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """额外授予的全站权限键（协议 PermissionKey），基础成员权限不必写入。"""


class SiteUserPrefs(SQLModel, table=True):
    """协议 Prefs：内容模式、R18G、模糊、屏蔽/关注 tag、私信隐私、钉选榜单。"""

    __tablename__ = "site_user_prefs"

    user_id: str = Field(primary_key=True)
    """Fluxer 权威用户 ID。"""

    content_mode: str = Field(
        default="general", sa_column_kwargs={"server_default": "general"}
    )
    """general 或 r18；有效模式还要看 R18 资格，见 site_v1.mode。"""

    show_r18g: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    """有效模式为 r18 时是否显示 R18G。"""

    blur_r18: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    """R18 图片是否模糊显示（前端用）。"""

    blocked_tag_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """屏蔽的 tag ID（site_tag.id 的字符串）。"""

    followed_tag_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """关注的 tag ID。"""

    dm_privacy: str = Field(
        default="everyone", sa_column_kwargs={"server_default": "everyone"}
    )
    """everyone / friends / nobody。"""

    pinned_boards: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """钉到侧栏的榜单 ID（按显示顺序）。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最近一次修改时间。"""


class SiteRestrictedAccess(SQLModel, table=True):
    """协议 RestrictedAccess：R18 资格状态，由确认条款和站务撤销驱动。"""

    __tablename__ = "site_restricted_access"

    user_id: str = Field(primary_key=True)
    """Fluxer 权威用户 ID。"""

    state: str = Field(default="none", sa_column_kwargs={"server_default": "none"})
    """granted / none / revoked；revoked 不会因再次确认而恢复。"""

    source: str | None = None
    """授予来源，例如 terms_confirm、discord_role、staff。"""

    checked_at: datetime | None = None
    """最近一次评估资格的时间。"""

    age_confirmed_at: datetime | None = None
    """确认满 18 岁的时间。"""

    terms_version: str | None = None
    """确认时的条款版本。"""

    revoked_by: str | None = None
    """撤销资格的站务用户 ID。"""

    revoked_at: datetime | None = None
    """撤销时间。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最近一次修改时间。"""


class SiteUserBlock(SQLModel, table=True):
    """账号屏蔽用户：所有列表不再出现对方的作品（协议 3.12）。"""

    __tablename__ = "site_user_block"
    __table_args__ = (Index("ix_site_user_block_blocked", "blocked_user_id"),)

    user_id: str = Field(primary_key=True)
    """发起屏蔽的用户 ID。"""

    blocked_user_id: str = Field(primary_key=True)
    """被屏蔽的用户 ID。"""

    created_at: datetime = Field(default_factory=utc_now)
    """屏蔽时间。"""


class SiteFakeCredential(SQLModel, table=True):
    """只在 FLUXER_FAKE=1 时使用：假 Fluxer 的用户名、邮箱和密码哈希。

    真实环境的密码只在 Fluxer 里，Forum 不持有；这张表在真实环境保持为空。
    """

    __tablename__ = "site_fake_credential"
    __table_args__ = (
        Index("uq_site_fake_credential_username", text("lower(username)"), unique=True),
        Index(
            "uq_site_fake_credential_email",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    user_id: str = Field(primary_key=True)
    """假 Fluxer 分配的用户 ID（数字字符串，形状同真实雪花 ID）。"""

    username: str
    """登录用户名。"""

    email: str | None = None
    """登录邮箱，可空。"""

    password_hash: str
    """scrypt 哈希，格式 scrypt$n$r$p$salt$hash。"""

    created_at: datetime = Field(default_factory=utc_now)
    """注册时间。"""


class SiteSetting(SQLModel, table=True):
    """站点级键值设置，例如 tag 目录版本号。"""

    __tablename__ = "site_setting"

    key: str = Field(primary_key=True)
    """设置名。"""

    value: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    """设置值（JSON 对象）。"""

    updated_at: datetime = Field(default_factory=utc_now)
    """最近一次修改时间。"""
