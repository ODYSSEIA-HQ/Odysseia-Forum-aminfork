"""好友（协议 3.8）：GET/POST /friends、DELETE /friends/{userId}、GET/POST /friends/requests、
DELETE /friends/requests/{id}。写法按假后端：POST /friends {requestId} 是接受申请。"""

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select

from models.site_me import SiteFriend, SiteFriendRequest
from models.user import User
from site_v1.deps import CurrentUser, get_session_factory
from site_v1.dto import iso_utc, load_users
from site_v1.errors import conflict, invalid, not_found
from site_v1.paging import PageRequest, page_response, pagination
from site_v1.services import friends as svc
from site_v1.services import social
from site_v1.services.notifications import add_notification

router = APIRouter(prefix="/friends", tags=["私信好友"])


@router.get("", summary="好友列表（新加的在前）")
async def list_friends(viewer: CurrentUser, page: PageRequest = Depends(pagination)):
    me = viewer.user_id
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(SiteFriend)
                    .where(or_(SiteFriend.user_a == me, SiteFriend.user_b == me))
                    .order_by(
                        SiteFriend.since.desc(), SiteFriend.user_a, SiteFriend.user_b
                    )
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        other = {
            (r.user_a, r.user_b): r.user_b if r.user_a == me else r.user_a for r in rows
        }
        users = await load_users(session, other.values())

    def dto(r):
        uid = other[(r.user_a, r.user_b)]
        return {"user": users.get(uid), "since": iso_utc(r.since)}

    result = page_response(rows, page, dto)
    # 已注销（合并）的账号不出现
    result["items"] = [i for i in result["items"] if i["user"] is not None]
    return result


@router.delete("/{userId}", summary="删除好友", status_code=204)
async def delete_friend(userId: str, viewer: CurrentUser):
    async with get_session_factory()() as session:
        await svc.remove_friend(session, viewer.user_id, userId)
        await session.commit()
    return Response(status_code=204)


def _request_dto(r: SiteFriendRequest, users: dict) -> dict:
    return {
        "id": str(r.id),
        "from": users.get(r.from_id),
        "to": users.get(r.to_id),
        "createdAt": iso_utc(r.created_at),
    }


@router.get("/requests", summary="好友申请 {incoming, outgoing}")
async def list_requests(viewer: CurrentUser):
    me = viewer.user_id
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(SiteFriendRequest)
                    .where(
                        or_(
                            SiteFriendRequest.from_id == me,
                            SiteFriendRequest.to_id == me,
                        )
                    )
                    .order_by(
                        SiteFriendRequest.created_at.desc(), SiteFriendRequest.id.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
        users = await load_users(
            session, {u for r in rows for u in (r.from_id, r.to_id)}
        )
    items = [
        (r, _request_dto(r, users))
        for r in rows
        if r.from_id in users and r.to_id in users
    ]
    return {
        "incoming": [d for r, d in items if r.to_id == me],
        "outgoing": [d for r, d in items if r.from_id == me],
    }


@router.post(
    "/requests", summary="发好友申请 {userId}（也接受用户名）", status_code=201
)
async def send_request(viewer: CurrentUser, body: dict = Body(...)):
    me = viewer.user_id
    raw = body.get("userId") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise invalid({"userId": "请填写对方的用户名或用户 ID"})
    key = raw.strip().lstrip("@")
    async with get_session_factory()() as session:
        peer = (
            (
                await session.execute(
                    select(User).where(
                        or_(User.id == key, User.username == key),
                        User.merged_into_id.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if peer is None:
            raise invalid({"userId": "找不到这个用户"})
        if peer.id == me:
            raise invalid({"userId": "不能加自己为好友"})
        if await svc.are_friends(session, me, peer.id):
            raise conflict("你们已经是好友了")
        # 屏蔽关系里的两个人不能互加；对外和「找不到」一样，不暴露被屏蔽
        if await social.is_blocked(session, me, peer.id) or await social.is_blocked(
            session, peer.id, me
        ):
            raise invalid({"userId": "找不到这个用户"})
        existing = await svc.request_between(session, me, peer.id)
        if existing is not None and existing.from_id == peer.id:
            raise conflict("对方已经向你发了好友申请，去「待处理」里接受吧")
        status = 200
        if existing is None:
            existing = SiteFriendRequest(from_id=me, to_id=peer.id)
            session.add(existing)
            await session.flush()
            add_notification(
                session,
                user_id=peer.id,
                category="interaction",
                actor_id=me,
                target={
                    "type": "friend_request",
                    "id": str(existing.id),
                    "title": viewer.display_name or viewer.username,
                },
                excerpt="想加你为好友",
            )
            status = 201
        users = await load_users(session, [me, peer.id])
        result = _request_dto(existing, users)
        await session.commit()
    return JSONResponse(status_code=status, content=result)


@router.post("", summary="接受好友申请 {requestId}", status_code=201)
async def accept_request(viewer: CurrentUser, body: dict = Body(...)):
    me = viewer.user_id
    request_id = body.get("requestId") if isinstance(body, dict) else None
    if not isinstance(request_id, str):
        raise invalid({"requestId": "缺少 requestId"})
    async with get_session_factory()() as session:
        request = (
            await session.get(SiteFriendRequest, int(request_id), with_for_update=True)
            if request_id.isdigit()
            else None
        )
        if request is None or request.to_id != me:
            raise not_found()
        peer = request.from_id
        row = await svc.make_friends(session, me, peer)
        add_notification(
            session,
            user_id=peer,
            category="interaction",
            actor_id=me,
            target={
                "type": "user",
                "id": me,
                "title": viewer.display_name or viewer.username,
            },
            excerpt="通过了你的好友申请",
        )
        users = await load_users(session, [peer])
        result = {"user": users.get(peer), "since": iso_utc(row.since)}
        await session.commit()
    return JSONResponse(status_code=201, content=result)


@router.delete("/requests/{id}", summary="拒绝或撤回好友申请", status_code=204)
async def delete_request(id: str, viewer: CurrentUser):
    me = viewer.user_id
    async with get_session_factory()() as session:
        request = (
            await session.get(SiteFriendRequest, int(id)) if id.isdigit() else None
        )
        if request is None or me not in (request.from_id, request.to_id):
            raise not_found()
        await session.delete(request)
        await session.commit()
    return Response(status_code=204)
