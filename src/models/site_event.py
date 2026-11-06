"""协议活动（3.22，草案 L）：活动、参赛条目、关注、活动日志。

- 状态由时间推出（见 site_v1/services/events.event_state）：manual_state 只存 draft / published / cancelled；
- 分组、奖项、资格、结果草稿是活动的一部分，整体读写，存 JSON（ID 是字符串，如 ed_xxxx、ea_xxxx）；
- 条目只记状态，不删（撤回 = withdrawn），截止后锁定 version_id。
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, Index, Text
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class SiteEvent(SQLModel, table=True):
    """一个活动。"""

    __tablename__ = "site_event"

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """活动 ID。"""

    slug: str = Field(index=True, unique=True)
    """短名（地址里也能用）。"""

    title: str = ""
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    body: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    rules: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))

    cover_attachment_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """封面附件（必须是全年龄图）。"""

    audience: str = Field(default="all", sa_column_kwargs={"server_default": "all"})
    """all / r18（r18 只对有效模式 r18 可见）。"""

    manual_state: str = Field(default="draft", sa_column_kwargs={"server_default": "draft"})
    """draft / published / cancelled；其余状态由时间推出。"""

    submit_start_at: datetime
    submit_end_at: datetime
    result_at: datetime | None = None

    eligibility: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    """协议 EventEligibility（camelCase 原样存）。"""

    divisions: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    """[{id, name, ratings}]。"""

    awards: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    """[{id, divisionId, name, description, order, decorRoleId}]。"""

    organizer_ids: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    """主办人用户 ID（最多 5 个）。"""

    results_draft: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    """结果草稿 [{awardId, entryIds, notes}]，对外不可见。"""

    results_published_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SiteEventEntry(SQLModel, table=True):
    """参赛条目。"""

    __tablename__ = "site_event_entry"
    __table_args__ = (
        Index("ix_site_event_entry_event", "event_id", "state"),
        Index("ix_site_event_entry_user", "submitted_by"),
        Index("ix_site_event_entry_work", "work_id"),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    event_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    division_id: str
    work_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    submitted_by: str
    note: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    version_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    """截止时锁定的版本。"""

    state: str = Field(default="accepted", sa_column_kwargs={"server_default": "accepted"})
    """pending / accepted / rejected / withdrawn / disqualified。"""

    state_reason: str | None = None
    award_id: str | None = None
    """公布结果时写入（公布前不对外）。"""

    award_note: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    submitted_at: datetime = Field(default_factory=utc_now)


class SiteEventFollow(SQLModel, table=True):
    """关注活动。"""

    __tablename__ = "site_event_follow"

    event_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    user_id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=utc_now)


class SiteEventLog(SQLModel, table=True):
    """活动日志（主办人和站务能看）。"""

    __tablename__ = "site_event_log"
    __table_args__ = (Index("ix_site_event_log_event", "event_id", "at"),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    event_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    actor_id: str | None = None
    action: str
    detail: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    work_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    """涉及的作品（按查看者有效模式过滤日志用）。"""

    at: datetime = Field(default_factory=utc_now)
