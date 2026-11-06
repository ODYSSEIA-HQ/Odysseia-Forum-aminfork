"""聊天面板接口（协议 3.5「NodeBB 路线参考接口」）、私信会话（3.8）、作品内禁言。

三种目标同一组接口（前端 src/features/chat-nodebb/api.ts）：
  作品交流区 /works/{id}/chat/...、私信 /conversations/{id}/...、公共聊天区 /channels/{id}/chat/...
  GET/POST  .../messages                      历史（before/after/around/limit；updatedAfter 另给改过的消息）、发送
  PATCH/DELETE .../messages/{mid}             编辑、删除（软删除留占位）
  PUT/DELETE .../messages/{mid}/reactions/{emoji}
  GET .../state、POST .../read、POST .../typing
实现在 services/chat_messages.py（经 FluxerPort.chat_* 代转 Fluxer 消息）。
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from models.user import User
from models.work_user_mute import WorkUserMute
from shared.time_utils import utc_now
from site_v1.config import get_config
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import iso_utc
from site_v1.errors import invalid
from site_v1.paging import PageRequest, pagination
from site_v1.services import chat_messages as svc
from site_v1.services.work_access import get_editable_work

router = APIRouter(tags=["聊天"])

BASES = {
    "work": "/works/{id}/chat",
    "dm": "/conversations/{id}",
    "channel": "/channels/{id}/chat",
}


def _register(kind: str, base: str) -> None:
    async def get_messages(
        id: str,
        ctx: Ctx,
        before: str | None = Query(None),
        after: str | None = Query(None),
        around: str | None = Query(None),
        limit: str | None = Query(None),
        updatedAfter: str | None = Query(None),
    ):
        async with get_session_factory()() as session:
            return await svc.list_messages(
                session,
                ctx,
                kind,
                id,
                before=before,
                after=after,
                around=around,
                limit=limit,
                updated_after=updatedAfter,
            )

    async def post_message(
        id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(default_factory=dict)
    ):
        async with get_session_factory()() as session:
            status, data = await svc.send_message(session, ctx, kind, id, body)
        return JSONResponse(data, status_code=status)

    async def patch_message(
        id: str,
        mid: str,
        viewer: CurrentUser,
        ctx: Ctx,
        body: dict = Body(default_factory=dict),
    ):
        async with get_session_factory()() as session:
            return await svc.edit_message(session, ctx, kind, id, mid, body)

    async def delete_message(id: str, mid: str, viewer: CurrentUser, ctx: Ctx):
        async with get_session_factory()() as session:
            return await svc.delete_message(session, ctx, kind, id, mid)

    async def put_reaction(
        id: str, mid: str, emoji: str, viewer: CurrentUser, ctx: Ctx
    ):
        async with get_session_factory()() as session:
            return await svc.react(session, ctx, kind, id, mid, emoji, True)

    async def delete_reaction(
        id: str, mid: str, emoji: str, viewer: CurrentUser, ctx: Ctx
    ):
        async with get_session_factory()() as session:
            return await svc.react(session, ctx, kind, id, mid, emoji, False)

    async def get_state(id: str, ctx: Ctx):
        async with get_session_factory()() as session:
            return await svc.get_state(session, ctx, kind, id)

    async def post_read(
        id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(default_factory=dict)
    ):
        async with get_session_factory()() as session:
            return await svc.mark_read(session, ctx, kind, id, body)

    async def post_typing(id: str, viewer: CurrentUser, ctx: Ctx):
        async with get_session_factory()() as session:
            await svc.typing(session, ctx, kind, id)
        return Response(status_code=204)

    name = {"work": "作品交流区", "dm": "私信", "channel": "公共聊天区"}[kind]
    router.add_api_route(
        f"{base}/messages", get_messages, methods=["GET"], summary=f"{name}：历史消息"
    )
    router.add_api_route(
        f"{base}/messages",
        post_message,
        methods=["POST"],
        status_code=201,
        summary=f"{name}：发消息",
    )
    router.add_api_route(
        f"{base}/messages/{{mid}}",
        patch_message,
        methods=["PATCH"],
        summary=f"{name}：编辑消息",
    )
    router.add_api_route(
        f"{base}/messages/{{mid}}",
        delete_message,
        methods=["DELETE"],
        summary=f"{name}：删除消息",
    )
    router.add_api_route(
        f"{base}/messages/{{mid}}/reactions/{{emoji}}",
        put_reaction,
        methods=["PUT"],
        summary=f"{name}：加表情",
    )
    router.add_api_route(
        f"{base}/messages/{{mid}}/reactions/{{emoji}}",
        delete_reaction,
        methods=["DELETE"],
        summary=f"{name}：取消表情",
    )
    router.add_api_route(
        f"{base}/state", get_state, methods=["GET"], summary=f"{name}：已读和禁言状态"
    )
    router.add_api_route(
        f"{base}/read", post_read, methods=["POST"], summary=f"{name}：标记已读"
    )
    router.add_api_route(
        f"{base}/typing",
        post_typing,
        methods=["POST"],
        status_code=204,
        summary=f"{name}：正在输入",
    )


for _kind, _base in BASES.items():
    _register(_kind, _base)


# ---------------- 私信会话（3.8） ----------------


@router.get("/conversations", summary="会话列表（私聊页左栏）")
async def get_conversations(
    viewer: CurrentUser, ctx: Ctx, page: PageRequest = Depends(pagination)
):
    async with get_session_factory()() as session:
        return await svc.list_conversations(session, ctx, page)


@router.post("/conversations", summary="新建私信（已有就返回原来的）", status_code=201)
async def post_conversation(
    viewer: CurrentUser, ctx: Ctx, body: dict = Body(default_factory=dict)
):
    async with get_session_factory()() as session:
        status, data = await svc.create_conversation(session, ctx, body)
    return JSONResponse(data, status_code=status)


# ---------------- 作品内禁言（3.5） ----------------


def _parse_until(raw) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise invalid(
            {"until": "请填写禁言截止时间（ISO 时间），或 null 表示直到手动解除"}
        )
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise invalid(
            {"until": "请填写禁言截止时间（ISO 时间），或 null 表示直到手动解除"}
        )
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    if value <= utc_now():
        raise invalid({"until": "截止时间要晚于现在"})
    return value


@router.post(
    "/works/{id}/mutes", summary="在本作品禁言（作者 / 卡贴管理员）", status_code=201
)
async def post_work_mute(
    id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(default_factory=dict)
):
    body = body if isinstance(body, dict) else {}
    async with get_session_factory()() as session:
        work = await get_editable_work(session, ctx, id, for_update=False)
        work_id = work.id
        user_id = body.get("userId")
        target = (
            await session.get(User, user_id)
            if isinstance(user_id, str) and len(user_id) <= 64
            else None
        )
        if target is None or target.merged_into_id:
            raise invalid({"userId": "用户不存在"})
        if user_id == viewer.user_id:
            raise invalid({"userId": "不能禁言自己"})
        if user_id == work.author_id:
            raise invalid({"userId": "不能禁言作品作者"})
        if "until" not in body:
            raise invalid(
                {"until": "请填写禁言截止时间（ISO 时间），或 null 表示直到手动解除"}
            )
        until = _parse_until(body.get("until"))
        reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
        reason = reason.strip()[:200]
        if viewer.discord_user_id and not get_config().fluxer_fake:
            # 真实环境走 V2（同时排 forum_work_mute 策略任务，把禁言同步到 Fluxer 原生帖）
            from work.work_moderation_service import WorkModerationService

            principal = viewer.to_v2_principal(ctx.effective_mode)
            await WorkModerationService(session).mute(
                work, principal, user_id, reason or "作品内禁言", expires_at=until
            )
        else:
            rows = (
                (
                    await session.execute(
                        select(WorkUserMute)
                        .where(
                            WorkUserMute.work_id == work.id,
                            WorkUserMute.user_id == user_id,
                            WorkUserMute.actor_id == viewer.user_id,
                            WorkUserMute.revoked_at.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if rows:
                rows[0].reason = reason or "作品内禁言"
                rows[0].expires_at = until
            else:
                session.add(
                    WorkUserMute(
                        work_id=work.id,
                        user_id=user_id,
                        actor_id=viewer.user_id,
                        reason=reason or "作品内禁言",
                        expires_at=until,
                    )
                )
        await session.commit()
    return JSONResponse(
        {
            "workId": str(work_id),
            "userId": user_id,
            "until": iso_utc(until),
            "reason": reason,
        },
        status_code=201,
    )


@router.delete("/works/{id}/mutes/{user_id}", summary="解除本作品禁言", status_code=204)
async def delete_work_mute(id: str, user_id: str, viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        work = await get_editable_work(session, ctx, id, for_update=False)
        rows = (
            (
                await session.execute(
                    select(WorkUserMute).where(
                        WorkUserMute.work_id == work.id,
                        WorkUserMute.user_id == user_id,
                        WorkUserMute.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        now = utc_now()
        for row in rows:
            row.revoked_at = now
        await session.commit()
    return Response(status_code=204)
