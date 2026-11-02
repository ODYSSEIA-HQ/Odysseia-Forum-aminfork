from pydantic import BaseModel, Field, field_serializer

from shared.utc_datetime import UTCDateTime


class AuthorFollowState(BaseModel):
    """作者关注操作后的关系状态。"""

    author_id: int | str = Field(description="当前身份模式下的作者 ID")
    """当前身份模式下的作者 ID"""

    followed_at: UTCDateTime = Field(description="最近一次关注该作者的时间")
    """最近一次关注该作者的时间"""

    active: bool = Field(description="当前是否仍在关注该作者")
    """当前是否仍在关注该作者"""

    @field_serializer("author_id")
    def serialize_author_id(self, value: int | str) -> str:
        """将作者 ID 序列化为字符串。"""
        return str(value)
