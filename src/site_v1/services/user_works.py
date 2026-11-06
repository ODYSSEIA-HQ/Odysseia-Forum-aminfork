"""GET /users/{id}/works 的数据来源。

作品领域还没做：先返回空页。作品领域接手时实现 list_user_works（按有效模式、
屏蔽 tag/用户、showR18G 过滤，只给 status=published），users 路由不用改。
"""

from site_v1.context import SiteContext
from site_v1.paging import PageRequest, empty_page


async def list_user_works(
    ctx: SiteContext, author_id: str, page: PageRequest, sort: str
) -> dict:
    """返回协议 WorkPage {items, nextCursor}。sort 是 new / latest / favorites。"""
    return empty_page()
