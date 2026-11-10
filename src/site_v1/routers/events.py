"""协议 3.22 活动（草案 L）。规则在 services/events.py。

浏览：GET /events、/events/{id}、/events/{id}/entries、/events/{id}/results、POST/DELETE /events/{id}/follow
参赛：POST /events/{id}/entries、PATCH/DELETE /events/{id}/entries/{entryId}、GET /me/event-entries
主办人：PATCH /events/{id}、GET /events/{id}/entries/manage、POST .../review、PUT /events/{id}/results、
       GET /events/{id}/results/draft、GET /events/{id}/log
站务：POST/PATCH /admin/events、/admin/events/{id}/publish|cancel、PUT /admin/events/{id}/organizers、
     POST /events/{id}/results/publish
投票、打分是草稿，不实现。
"""

from fastapi import APIRouter, Body, Depends, Query, Response

from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.paging import PageRequest, pagination
from site_v1.services import events as svc

router = APIRouter(tags=["活动"])


async def run(fn, *args, **kwargs):
    """开会话 → 调服务 → 生成响应后提交（服务里只 flush）。"""
    async with get_session_factory()() as session:
        result = await fn(session, *args, **kwargs)
        await session.commit()
        return result


def _body(body: dict | None) -> dict:
    return body if isinstance(body, dict) else {}


# ---------------- 浏览 ----------------


@router.get("/events", summary="活动列表")
async def list_events(
    ctx: Ctx,
    state: str | None = Query(None),
    q: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    return await run(svc.list_events, ctx, state=state, q=q, page=page)


@router.get("/events/{id}", summary="活动页")
async def get_event(id: str, ctx: Ctx):
    async def go(session):
        return await svc.event_dto(session, ctx, await svc.visible_event(session, ctx, id))

    return await run(go)


@router.get("/events/{id}/entries/manage", summary="全部条目（主办人/站务）")
async def manage_entries(
    id: str, viewer: CurrentUser, ctx: Ctx,
    state: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    return await run(svc.manage_entries, ctx, id, state=state, page=page)


@router.get("/events/{id}/entries", summary="参赛作品列表（只含 accepted）")
async def list_entries(
    id: str, ctx: Ctx,
    division: str | None = Query(None),
    sort: str | None = Query(None),
    seed: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    return await run(svc.list_entries, ctx, id, division=division, sort=sort, seed=seed, page=page)


@router.get("/events/{id}/results", summary="结果（公布后）")
async def get_results(id: str, ctx: Ctx):
    return await run(svc.results, ctx, id)


@router.post("/events/{id}/follow", status_code=204, summary="关注活动")
async def follow(id: str, viewer: CurrentUser, ctx: Ctx):
    await run(svc.set_follow, ctx, id, True)
    return Response(status_code=204)


@router.delete("/events/{id}/follow", status_code=204, summary="取消关注活动")
async def unfollow(id: str, viewer: CurrentUser, ctx: Ctx):
    await run(svc.set_follow, ctx, id, False)
    return Response(status_code=204)


# ---------------- 参赛 ----------------


@router.post("/events/{id}/entries", status_code=201, summary="提交作品")
async def submit_entry(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.submit_entry, ctx, id, _body(body))


@router.patch("/events/{id}/entries/{entryId}", summary="改参赛说明（截止前）")
async def update_entry(id: str, entryId: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.update_entry, ctx, id, entryId, _body(body))


@router.delete("/events/{id}/entries/{entryId}", status_code=204, summary="撤回（截止前）")
async def withdraw_entry(id: str, entryId: str, viewer: CurrentUser, ctx: Ctx):
    await run(svc.withdraw_entry, ctx, id, entryId)
    return Response(status_code=204)


@router.get("/me/event-entries", summary="我的参赛条目")
async def my_entries(viewer: CurrentUser, ctx: Ctx, page: PageRequest = Depends(pagination)):
    return await run(svc.my_entries, ctx, page)


# ---------------- 主办人 ----------------


@router.patch("/events/{id}", summary="改介绍、规则、封面（主办人/站务）")
async def patch_event(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.patch_event, ctx, id, _body(body))


@router.post("/events/{id}/entries/{entryId}/review", summary="处理条目")
async def review_entry(id: str, entryId: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.review_entry, ctx, id, entryId, _body(body))


@router.put("/events/{id}/results", summary="保存结果草稿")
async def save_results(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.save_results, ctx, id, _body(body))


@router.get("/events/{id}/results/draft", summary="读结果草稿（前端草案）")
async def results_draft(id: str, viewer: CurrentUser, ctx: Ctx):
    return await run(svc.get_results_draft, ctx, id)


@router.post("/events/{id}/results/publish", summary="公布结果（站务）")
async def publish_results(id: str, viewer: CurrentUser, ctx: Ctx):
    return await run(svc.publish_results, ctx, id)


@router.get("/events/{id}/log", summary="活动日志")
async def event_log(id: str, viewer: CurrentUser, ctx: Ctx, page: PageRequest = Depends(pagination)):
    return await run(svc.event_log, ctx, id, page)


# ---------------- 站务 ----------------


@router.post("/admin/events", status_code=201, summary="建活动（草稿）")
async def admin_create(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.admin_create, ctx, _body(body))


@router.patch("/admin/events/{id}", summary="改活动设置")
async def admin_update(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.admin_update, ctx, id, _body(body))


@router.post("/admin/events/{id}/publish", summary="发布活动")
async def admin_publish(id: str, viewer: CurrentUser, ctx: Ctx):
    return await run(svc.admin_publish, ctx, id)


@router.post("/admin/events/{id}/cancel", summary="取消活动")
async def admin_cancel(id: str, viewer: CurrentUser, ctx: Ctx, body: dict | None = Body(None)):
    return await run(svc.admin_cancel, ctx, id, _body(body))


@router.put("/admin/events/{id}/organizers", summary="指定主办人")
async def admin_organizers(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    return await run(svc.admin_organizers, ctx, id, _body(body))
