"""在线状态（协议 0.5 可选能力 presence，GET /site 的 features 里列出）。

  PUT/DELETE /works/{id}/chat/presence、/channels/{id}/chat/presence   心跳 / 离开
  GET /works/{id}/chat/viewers、/channels/{id}/chat/viewers           正在看的人
  GET /users/{id}/presence                                            某人在不在线
  GET/PUT /me/presence-settings                                       我的偏好
实现在 services/presence.py（Redis）。
"""

from fastapi import APIRouter, Body
from fastapi.responses import Response

from models.user import User
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.errors import not_found
from site_v1.services import chat_messages as chat
from site_v1.services import presence as svc

router = APIRouter(tags=["在线状态"])

BASES = {"work": "/works/{id}/chat", "channel": "/channels/{id}/chat"}


def _register(kind: str, base: str) -> None:
    name = {"work": "作品交流区", "channel": "公共聊天区"}[kind]

    async def put_presence(id: str, viewer: CurrentUser, ctx: Ctx):
        async with get_session_factory()() as session:
            chan = await chat.resolve(session, ctx, kind, id)
        await svc.heartbeat(chan, viewer.user_id)
        return Response(status_code=204)

    async def delete_presence(id: str, viewer: CurrentUser, ctx: Ctx):
        async with get_session_factory()() as session:
            chan = await chat.resolve(session, ctx, kind, id)
        await svc.leave(chan, viewer.user_id)
        return Response(status_code=204)

    async def get_viewers(id: str, ctx: Ctx):
        async with get_session_factory()() as session:
            chan = await chat.resolve(session, ctx, kind, id)
            return await svc.viewers(session, ctx, chan)

    router.add_api_route(
        f"{base}/presence",
        put_presence,
        methods=["PUT"],
        status_code=204,
        summary=f"{name}：我正在看（心跳）",
    )
    router.add_api_route(
        f"{base}/presence",
        delete_presence,
        methods=["DELETE"],
        status_code=204,
        summary=f"{name}：我离开了",
    )
    router.add_api_route(
        f"{base}/viewers", get_viewers, methods=["GET"], summary=f"{name}：正在看的人"
    )


for _kind, _base in BASES.items():
    _register(_kind, _base)


@router.get("/users/{id}/presence", summary="某人在不在线")
async def get_user_presence(id: str, ctx: Ctx):
    async with get_session_factory()() as session:
        user = await session.get(User, id) if len(id) <= 64 else None
        if user is None or user.merged_into_id:
            raise not_found()
        return {"online": await svc.user_online(session, ctx, id)}


@router.get("/me/presence-settings", summary="我的在线状态偏好")
async def get_presence_settings(viewer: CurrentUser):
    return await svc.get_settings(viewer.user_id)


@router.put("/me/presence-settings", summary="改在线状态偏好")
async def put_presence_settings(
    viewer: CurrentUser, body: dict = Body(default_factory=dict)
):
    return await svc.put_settings(viewer.user_id, body)
