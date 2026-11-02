"""受信原生附件预上传描述与已存在附件引用。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NativeAttachment(BaseModel):
    """只传原生上传描述，不持久化用户Bearer。"""

    model_config = ConfigDict(extra="forbid")
    id: int | str
    """新上传使用本次整数槽位，保留附件使用既有雪花字符串。"""

    filename: str | None = Field(default=None, min_length=1, max_length=255)
    """新上传文件名。"""

    file_size: int | None = Field(default=None, ge=1)
    """新上传文件字节数。"""

    content_type: str | None = None
    """新上传内容类型。"""

    upload_filename: str | None = Field(default=None, min_length=1, max_length=2048)
    """由原生上传接口签发的作用域内文件定位。"""

    @model_validator(mode="after")
    def validate_reference(self):
        """既有附件只允许ID，新上传必须完整且不可伪造为保留附件。"""
        if isinstance(self.id, str):
            if (
                not self.id.isdecimal()
                or self.id.startswith("0")
                or any(
                    value is not None
                    for value in (
                        self.filename,
                        self.file_size,
                        self.content_type,
                        self.upload_filename,
                    )
                )
            ):
                raise ValueError("既有附件必须仅提供真实雪花id")
        elif (
            isinstance(self.id, bool)
            or self.id < 0
            or not self.filename
            or self.file_size is None
            or not self.upload_filename
        ):
            raise ValueError("新附件缺少原生预上传描述")
        return self
