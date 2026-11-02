from sqlalchemy import BigInteger, CheckConstraint, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


class TagVote(SQLModel, table=True):
    """保存用户对某轮自定义挂标的当前投票。"""

    __tablename__ = "tag_vote"

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )

    binding_id: int = Field(
        sa_column=Column(BigInteger, nullable=False),
        description="挂标轮次 ID，与用户 ID 组成联合主键；不是标签实体 ID",
    )
    """挂标轮次 ID，与用户 ID 组成联合主键；不是标签实体 ID"""

    user_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
        description="投票用户的 Discord ID；同一轮次每人最多一票",
    )
    """投票用户的 Discord ID；同一轮次每人最多一票"""

    vote: int = Field(
        description="当前票值：1 为赞，-1 为踩；撤票时删除此记录，变更历史保留在操作日志"
    )
    """当前票值：1 为赞，-1 为踩；撤票时删除此记录，变更历史保留在操作日志"""

    __table_args__ = (
        CheckConstraint("vote IN (-1, 1)", name="ck_tag_vote"),
        UniqueConstraint("binding_id", "user_id", name="uq_tag_vote_legacy"),
        UniqueConstraint("binding_id", "fluxer_user_id", name="uq_tag_vote_fluxer"),
    )

    fluxer_user_id: str | None = Field(
        default=None,
        index=True,
        description="对本轮标签绑定投票的用户 Fluxer ID",
    )
    """对本轮自定义标签绑定投票的用户 Fluxer ID，用于区分各用户在该轮的当前投票。"""
