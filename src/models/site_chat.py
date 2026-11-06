"""/api/v1 聊天（代转 Fluxer 消息）用到的表，迁移 site_v1_chat_tables。

两类：
- Forum 自己的叠加数据（真实、假 Fluxer 都用）：
  - site_chat_message_meta：经本站发出的消息挂的本站附件、所属目标、删除记录（审计、占位）；
  - site_chat_read_state：每个账号在每个频道读到哪条（GET .../state、未读数）。
- 假 Fluxer 的存储（只在 FLUXER_FAKE=1 时有数据，真实环境消息在 Fluxer）：
  - site_chat_message：消息；
  - site_chat_dm_channel：私信频道（两人一对）。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Column, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class SiteChatMessageMeta(SQLModel, table=True):
    """经本站发出 / 本站处理过的消息的叠加数据。"""

    __tablename__ = "site_chat_message_meta"

    channel_id: str = Field(sa_column=Column(String(64), primary_key=True))
    """Fluxer 频道 ID（原生主帖、私信频道、公共频道）。"""

    message_id: str = Field(sa_column=Column(String(64), primary_key=True))
    """Fluxer 消息 ID。"""

    author_id: str | None = Field(default=None, sa_column=Column(String(64)))
    target_kind: str = Field(
        default="work", sa_column=Column(String(16), nullable=False)
    )
    """work / dm / channel。"""

    target_id: str = Field(default="", sa_column=Column(String(64), nullable=False))
    """协议里的目标 ID：作品 ID、会话 ID（= 私信频道 ID）、公共频道 ID。"""

    attachment_ids: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )
    """本站附件（attachment.id），按发送顺序。"""

    deleted_state: str | None = Field(default=None, sa_column=Column(String(16)))
    """by_author / by_manager / by_staff。"""

    deleted_by: str | None = Field(default=None, sa_column=Column(String(64)))
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SiteChatReadState(SQLModel, table=True):
    """已读位置：只往后走。"""

    __tablename__ = "site_chat_read_state"
    __table_args__ = (
        Index("ix_site_chat_read_state_target", "target_kind", "target_id"),
    )

    user_id: str = Field(sa_column=Column(String(64), primary_key=True))
    channel_id: str = Field(sa_column=Column(String(64), primary_key=True))
    target_kind: str = Field(sa_column=Column(String(16), nullable=False))
    target_id: str = Field(sa_column=Column(String(64), nullable=False))
    last_read_message_id: str = Field(sa_column=Column(String(64), nullable=False))
    updated_at: datetime = Field(default_factory=utc_now)


class SiteChatMessage(SQLModel, table=True):
    """假 Fluxer 的消息（真实环境不用）。"""

    __tablename__ = "site_chat_message"
    __table_args__ = (Index("ix_site_chat_message_channel_id", "channel_id", "id"),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    channel_id: str = Field(sa_column=Column(String(64), nullable=False))
    author_id: str = Field(sa_column=Column(String(64), nullable=False))
    content: str = Field(
        default="", sa_column=Column(Text, nullable=False, server_default="")
    )
    reply_to: int | None = Field(default=None, sa_column=Column(BigInteger))
    nonce: str | None = Field(default=None, sa_column=Column(String(64)))
    reactions: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    """{emoji: [user_id, ...]}，按第一次出现的顺序。"""

    edited_at: datetime | None = None
    deleted_state: str | None = Field(default=None, sa_column=Column(String(16)))
    created_at: datetime = Field(default_factory=utc_now)


class SiteChatDmChannel(SQLModel, table=True):
    """假 Fluxer 的私信频道：一对账号一个（user_low < user_high）。"""

    __tablename__ = "site_chat_dm_channel"
    __table_args__ = (
        UniqueConstraint("user_low", "user_high", name="uq_site_chat_dm_pair"),
        Index("ix_site_chat_dm_channel_user_high", "user_high"),
    )

    id: str = Field(sa_column=Column(String(64), primary_key=True))
    user_low: str = Field(sa_column=Column(String(64), nullable=False))
    user_high: str = Field(sa_column=Column(String(64), nullable=False))
    created_at: datetime = Field(default_factory=utc_now)
