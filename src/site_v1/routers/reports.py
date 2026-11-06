"""协议 3.10：POST /reports。

规则见 services/moderation.py：理由按对象类型限定、看不到的对象 404、
同一账号对同一待处理对象重复举报 409（report 表的部分唯一索引兜底）。
"""

from fastapi import APIRouter, Body

from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.services import moderation

router = APIRouter(tags=["举报审核"])


@router.post("/reports", status_code=201, summary="举报")
async def post_report(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    async with get_session_factory()() as session:
        return await moderation.create_report(session, ctx, body)
