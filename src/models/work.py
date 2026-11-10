"""跨平台作品的统一内容投影。"""

from datetime import datetime
from sqlalchemy import BigInteger, Column, JSON, Index, event, inspect, func, text, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import TSVECTOR
from shared.text_utils import build_search_vector_text
from sqlmodel import SQLModel, Field
from shared.time_utils import utc_now


class Work(SQLModel, table=True):
    """保存作品内容与主来源，不独立存储封面。"""

    __table_args__ = (
        Index("ix_work_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_work_visible_created_id",
            text("created_at DESC NULLS LAST"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL AND source_visible IS TRUE"),
        ),
        Index(
            "ix_work_author_created_id",
            "author_id",
            text("created_at DESC NULLS LAST"),
            text("id DESC"),
        ),
    )

    search_vector: str | None = Field(
        default=None, sa_column=Column(TSVECTOR, nullable=True)
    )
    """与既有搜索相同的 rjieba simple 全文向量。"""

    last_active_at: datetime | None = Field(default=None, index=True)
    """所有来源帖子最新活跃时间。"""

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    """作品内部编号，对外序列化为字符串。"""

    title: str
    """作品标题。"""

    description: str | None = None
    """作品简介。"""

    classification_revision: int = Field(
        default=0, sa_column=Column(BigInteger, nullable=False)
    )
    """作品期望分类的单调版本，异步移动任务据此排序。"""

    source_visible: bool = True
    """来源暂不可见时隐藏作品，但不伪造成权威删除。"""

    metadata_overrides: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    """用户显式覆盖的作品元数据，不被来源空快照清除。"""

    images: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    """作品图片，首图为展示封面。"""

    author_id: str | None = Field(default=None, index=True)
    """统一 Fluxer 作者编号。"""

    source_platform: str = "discord"
    """主来源平台。"""

    source_guild_id: str = ""
    """主来源服务器编号。"""

    source_channel_id: str = ""
    """主来源频道编号。"""

    source_thread_id: str = ""
    """主来源帖子编号。"""

    main_ref: str = ""
    """主来源链接。"""

    is_nsfw: bool = Field(default=False, index=True)
    """是否为成人分区。"""

    is_abyss: bool = Field(default=False, index=True)
    """是否为深渊分区。"""

    created_at: datetime = Field(default_factory=utc_now, index=True)
    """作品创建时间。"""

    deleted_at: datetime | None = Field(default=None, index=True)
    """软删除时间。"""

    deletion_reason: str | None = None
    """原生主帖删除不可恢复，其余为可管理恢复的隐藏。"""

    # ---- 以下是协议 /api/v1 层（site_v1）加的字段，V1/V2 不读不写 ----
    # 全部带 server_default：thread 投影触发器 project_thread_work 按列名插入 work，
    # 不认识这些列，靠数据库默认值补齐。

    status: str = Field(
        default="published",
        sa_column=Column(String, nullable=False, server_default="published", index=True
        ),
    )
    """协议 WorkStatus：draft / published / archived / unclaimed / removed。
    V2 和 DC 来的作品一律视为 published；/api/v1 新建的草稿显式写 draft。"""

    summary: str = Field(
        default="", sa_column=Column(String, nullable=False, server_default="")
    )
    """一句话简介（协议 Work.summary），和 description（正文）分开。"""

    layout: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """作者自定义的介绍页布局，后端原样存。"""

    cover_attachment_id: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    """封面附件 attachment.id；V2 作品仍用 images 首图。"""

    gallery_attachment_ids: list[int] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """图库附件 ID（有序）。"""

    declarations: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    """必答问题的回答记录 [{question, answer, versionId, at}]。"""

    rating_lock: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    """站务锁定分级 {by, at, reason}。"""

    moderation_state: str = Field(
        default="ok",
        sa_column=Column(String, nullable=False, server_default="ok"),
    )
    """ok / pending_review / hidden / removed。"""

    tag_locked: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    """作者是否锁定了 tag。"""

    published_at: datetime | None = None
    """首次发布时间。"""

    updated_at: datetime = Field(
        default_factory=utc_now,
        sa_column=Column(DateTime, nullable=False, server_default=func.now()),
    )
    """协议 Work.updatedAt：作品资料最近一次修改时间。"""

    visibility: str = Field(
        default="public",
        sa_column=Column(String, nullable=False, server_default="public", index=True
        ),
    )
    """public / restricted 的冗余列，方便列表过滤；由分级和限制 tag 算出。"""


@event.listens_for(Work, "before_insert")
@event.listens_for(Work, "before_update")
def update_work_search_vector(mapper, connection, target):
    """作品标题或简介变化时使用与 Thread 相同的分词规则。"""
    state = inspect(target)
    if state.persistent and not (
        state.attrs.title.history.has_changes()
        or state.attrs.description.history.has_changes()
    ):
        return
    tokens = build_search_vector_text(target.title, target.description)
    target.search_vector = func.to_tsvector("simple", tokens) if tokens else None
