"""作品搜索沿用既有高级检索参数。"""

from pydantic import Field
from api.v1.schemas.search.search_request import SearchRequest


class WorkSearchRequest(SearchRequest):
    """扩展作品分类与逻辑编号，既有关键词语法和排序不变。"""

    is_nsfw: bool | None = None
    """成人分区筛选，不覆盖全局隐藏偏好。"""

    is_abyss: bool | None = None
    """深渊分区筛选，不覆盖身份门槛。"""

    platform: str | None = None
    """主来源平台。"""

    exclude_work_ids: list[str] = Field(default_factory=list, max_length=1000)
    """已显示的作品内部编号。"""
