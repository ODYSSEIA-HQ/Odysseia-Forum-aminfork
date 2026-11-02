"""V1 与作品搜索共享偏好覆盖规则。"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.v1.schemas.search import SearchRequest
    from dto.preferences import UserSearchPreferencesDTO


def merge_search_preferences(request: SearchRequest, prefs: UserSearchPreferencesDTO):
    """
    将用户偏好合并到搜索请求中。
    原则：仅当前端未显示传递（未出现在 unset 列表中）
    且偏好有值时，才使用偏好值覆盖。
    """
    # 获取前端明确传递的字段集合
    explicit_fields = request.model_dump(exclude_unset=True)

    # 映射表: 偏好字段名 -> 请求对象字段名
    mapping = {
        "preferred_channels": "channel_ids",
        "include_authors": "include_authors",
        "exclude_authors": "exclude_authors",
        "include_tags": "include_tags",
        "exclude_tags": "exclude_tags",
        "include_keywords": "keywords",
        "exclude_keywords": "exclude_keywords",
        "exclude_keyword_exemption_markers": ("exclude_keyword_exemption_markers"),
        "sort_method": "sort_method",
        "custom_base_sort": "custom_base_sort",
        "created_after": "created_after",
        "created_before": "created_before",
        "active_after": "active_after",
        "active_before": "active_before",
    }

    for pref_key, req_key in mapping.items():
        # 如果前端没传这个参数
        if req_key not in explicit_fields:
            pref_value = getattr(prefs, pref_key, None)
            # 如果偏好设置中有非空有效值，则覆盖默认值
            if pref_value is not None and pref_value != "":
                if req_key in ("include_authors", "exclude_authors"):
                    from identity.runtime import uses_canonical_storage
                    from identity.canonical_user_id import CanonicalUserId

                    if uses_canonical_storage():
                        pref_value = [
                            CanonicalUserId(str(value)) for value in pref_value
                        ]
                setattr(request, req_key, pref_value)
