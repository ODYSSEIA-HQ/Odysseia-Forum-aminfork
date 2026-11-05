"""V2 自身搜索偏好协议，身份和服务器作用域由认证入口决定。"""

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
)
from shared.time_parser import parse_time_string

Sort = Literal[
    "comprehensive",
    "created_at",
    "last_active",
    "last_active_at",
    "reaction_count",
    "reply_count",
    "collection_count",
    "view_count",
    "collected_at",
    "reddit_hot",
    "custom",
]
BaseSort = Literal[
    "comprehensive",
    "created_at",
    "last_active",
    "last_active_at",
    "reaction_count",
    "reply_count",
    "collection_count",
    "view_count",
    "collected_at",
    "reddit_hot",
]


class WorkPreferencesUpdateRequest(BaseModel):
    """省略保留原值，null 清除筛选；成人内容单独提交并可携带 CAS 版本。"""

    model_config = ConfigDict(extra="forbid")

    preferred_channels: list[int] | None = Field(default=None, max_length=500)
    """优先显示的 Discord 来源频道编号。"""

    include_authors: list[str] | None = Field(default=None, max_length=500)
    """仅包含的统一作者编号。"""

    exclude_authors: list[str] | None = Field(default=None, max_length=500)
    """排除的统一作者编号。"""

    include_tags: list[str] | None = Field(default=None, max_length=500)
    """必须匹配的标签。"""

    exclude_tags: list[str] | None = Field(default=None, max_length=500)
    """需要排除的标签。"""

    include_keywords: str | None = Field(default=None, max_length=10000)
    """需要匹配的关键词表达式。"""

    exclude_keywords: str | None = Field(default=None, max_length=10000)
    """需要排除的关键词表达式。"""

    exclude_keyword_exemption_markers: list[str] | None = Field(
        default=None, max_length=100
    )
    """允许绕过排除关键词的标记。"""

    preview_image_mode: Literal["thumbnail", "full", "none"] | None = None
    """搜索结果的图片预览模式。"""

    results_per_page: StrictInt | None = Field(default=None, ge=1, le=9)
    """机器人每页结果数量。"""

    ui_page_size: StrictInt | None = Field(default=None, ge=1, le=100)
    """网页每页结果数量。"""

    sort_method: Sort | None = None
    """搜索排序方式。"""

    custom_base_sort: BaseSort | None = None
    """自定义排序使用的基础顺序。"""

    created_after: str | None = Field(default=None, max_length=100)
    """最早创建时间，支持绝对或相对日期。"""

    created_before: str | None = Field(default=None, max_length=100)
    """最晚创建时间，支持绝对或相对日期。"""

    active_after: str | None = Field(default=None, max_length=100)
    """最早活跃时间，支持绝对或相对日期。"""

    active_before: str | None = Field(default=None, max_length=100)
    """最晚活跃时间，支持绝对或相对日期。"""

    show_nsfw: StrictBool | None = None
    """用户选择的成人内容显示开关，不代表年龄资格。"""

    expected_version: StrictInt | None = Field(default=None, ge=0, le=2**53 - 2)
    """更新成人内容开关时预期的权威版本。"""

    @field_validator(
        "preferred_channels", "include_authors", "exclude_authors", mode="before"
    )
    @classmethod
    def ids(cls, value: Any, info):
        """拒绝布尔值、浮点和舍入后的 ID；响应始终以字符串传输雪花。"""
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("ID 必须是列表")
        # 统一输入后去重，保持用户提供的筛选顺序。
        result = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                raise ValueError("ID 必须是正整数或完整数字字符串")
            raw = str(item)
            if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 2**63:
                raise ValueError("ID 必须是有效的正整数雪花")
            normalized = (
                int(raw) if info.field_name == "preferred_channels" else str(int(raw))
            )
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator(
        "include_tags", "exclude_tags", "exclude_keyword_exemption_markers"
    )
    @classmethod
    def text_lists(cls, value):
        """清理标签空白、拒绝无效文本并保留首次出现顺序。"""
        if value is None:
            return None
        # 统一输入后去重，保持用户提供的筛选顺序。
        result = []
        for item in value:
            item = item.strip()
            if not item or len(item) > 200:
                raise ValueError("标签或豁免标记必须是 1–200 字符的非空文本")
            if item not in result:
                result.append(item)
        return result

    @field_validator("created_after", "created_before", "active_after", "active_before")
    @classmethod
    def dates(cls, value):
        """验证绝对或相对日期，并将空白筛选转换为空值。"""
        if value is None or not value.strip():
            return None
        # 沿用已有日期语法，避免新旧搜索的解析结果不同。
        value = value.strip()
        try:
            parse_time_string(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError("需要有效日期或相对时间，如 2026-11-05、-7d") from exc
        return value

    @field_validator("include_keywords", "exclude_keywords")
    @classmethod
    def keywords(cls, value):
        """显式空值清除关键词，省略字段时仍保留原值。"""
        return value if value is not None else ""

    @field_validator(
        "preview_image_mode",
        "results_per_page",
        "ui_page_size",
        "sort_method",
        "custom_base_sort",
        "show_nsfw",
        "expected_version",
    )
    @classmethod
    def non_null(cls, value):
        """显示、分页、排序和成人内容字段只接受明确值。"""
        if value is None:
            raise ValueError("此字段不能为 null；省略字段以保留原值")
        return value
