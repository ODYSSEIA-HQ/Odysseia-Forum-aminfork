"""GET /users/{id}/works 的数据来源：作者已发布的作品，按有效模式、showR18G、
屏蔽 tag/用户过滤（和 GET /works 同一套条件，见 services/works.listable_conditions）。"""

from models.work import Work
from site_v1.context import SiteContext
from site_v1.deps import get_session_factory
from site_v1.paging import PageRequest
from site_v1.services import works as svc


async def list_user_works(
    ctx: SiteContext, author_id: str, page: PageRequest, sort: str
) -> dict:
    """返回协议 WorkPage {items, nextCursor}。sort 是 new / latest / favorites。"""
    async with get_session_factory()() as session:
        conds = await svc.listable_conditions(session, ctx)
        conds.append(Work.author_id == author_id)
        return await svc.query_page(session, ctx, conds, page, sort)
