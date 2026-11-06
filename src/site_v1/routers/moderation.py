"""协议 3.10：GET /moderation/queue、POST /moderation/queue/{id}/resolve。

权限：image 队列要 moderation.images 且本人 R18 资格 granted；report 队列要
moderation.reports（或 moderation.content）。实现见 services/moderation.py。
"""

from fastapi import APIRouter, Body, Depends, Query

from site_v1.deps import CurrentUser, get_session_factory
from site_v1.errors import invalid
from site_v1.paging import PageRequest, pagination
from site_v1.services import moderation

router = APIRouter(tags=["举报审核"])


@router.get("/moderation/queue", summary="审核队列（站务）")
async def get_queue(
    viewer: CurrentUser,
    type: str | None = Query(None),
    state: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    fields = {}
    if type not in ("image", "report"):
        fields["type"] = "type 只能是 image 或 report"
    if state is not None and state not in ("pending", "resolved"):
        fields["state"] = "state 只能是 pending 或 resolved"
    if fields:
        raise invalid(fields)
    async with get_session_factory()() as session:
        return await moderation.list_queue(
            session, viewer, type, state or "pending", page
        )


@router.post("/moderation/queue/{id}/resolve", summary="处理队列项（站务）")
async def resolve_item(id: str, viewer: CurrentUser, body: dict = Body(...)):
    async with get_session_factory()() as session:
        return await moderation.resolve(session, viewer, id, body)
