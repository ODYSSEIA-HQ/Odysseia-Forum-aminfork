"""子卡区分页排序参数；不允许客户端扩大作品或身份范围。"""

from typing import Literal
from pydantic import BaseModel, Field

CardSort = Literal[
    "comprehensive",
    "reddit_hot",
    "created_at",
    "last_active",
    "last_active_at",
    "reaction_count",
    "reply_count",
    "collection_count",
]


class WorkCardsQuery(BaseModel):
    offset: int = Field(default=0, ge=0, le=100000)
    """分页起始位置。"""

    limit: int = Field(default=20, ge=1, le=100)
    """单页返回的最大子卡数量。"""

    sort_method: Literal[
        "comprehensive",
        "reddit_hot",
        "created_at",
        "last_active",
        "last_active_at",
        "reaction_count",
        "reply_count",
        "collection_count",
        "custom",
    ] = "comprehensive"
    """子卡排序方式，支持自定义基础排序。"""

    sort_order: Literal["asc", "desc"] = "desc"
    """排序方向。"""

    custom_base_sort: CardSort = "comprehensive"
    """自定义排序使用的基础算法。"""
