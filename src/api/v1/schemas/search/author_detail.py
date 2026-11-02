from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class AuthorDetail(BaseModel):
    """
    API 响应中作者的详细信息模型
    """

    id: int | str = Field(description="当前身份模式下的作者 ID")
    name: str = Field(description="作者的唯一用户名")
    global_name: Optional[str] = Field(description="作者的全局显示名称")
    display_name: str = Field(description="作者的服务器内显示名称")
    avatar_url: Optional[str] = Field(description="作者头像的 URL")

    @field_serializer("id")
    def serialize_id(self, value: int | str) -> str:
        """将作者 ID 序列化为字符串，避免 JavaScript 精度丢失"""
        return str(value)

    model_config = ConfigDict(from_attributes=True)
