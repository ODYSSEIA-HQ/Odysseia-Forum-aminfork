"""公共聊天区的频道目录和管理（10-28，【待定】协议还没有，以假后端 src/mock/handlers/chat.ts 为准）。

- GET /channels：当前有效模式下能看到的频道（全年龄模式只给 rating=general），按 order 排，返回数组；
- GET /admin/channels：站务后台用，不按模式过滤；
- POST /channels、PATCH /channels/{id}、DELETE /channels/{id}：站务（channels.manage）建、改、排序、删。

频道的聊天消息不在这里（聊天一块负责）。频道对象在 site_public_channel，fluxer_channel_id 记对应的
Fluxer 频道（可空）；lastMessageAt 要聊天一块按 Fluxer 频道的最后一条消息填，这里先给 null。
"""

import secrets
import re

from fastapi import APIRouter, Body
from fastapi.responses import Response
from sqlalchemy import func, select

from models.site_nav import SitePublicChannel
from shared.time_utils import utc_now
from site_v1.deps import Ctx, get_session_factory
from site_v1.errors import SiteError, forbidden, invalid, not_found

router = APIRouter(tags=["公共聊天区"])

CHANNEL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
RATINGS = ("general", "r18", "r18g")


def channel_dto(c: SitePublicChannel) -> dict:
    out = {
        "id": c.id,
        "name": c.name,
        "description": c.description or "",
        "rating": c.rating,
        "order": int(c.sort_order or 0),
        "lastMessageAt": None,
    }
    if c.rules:
        out["rules"] = c.rules
    return out


def rating_visible(rating: str, mode: str) -> bool:
    return rating == "general" or mode == "r18"


async def sorted_channels(
    session, *, for_update: bool = False
) -> list[SitePublicChannel]:
    q = select(SitePublicChannel).order_by(
        SitePublicChannel.sort_order, SitePublicChannel.created_at, SitePublicChannel.id
    )
    if for_update:
        q = q.with_for_update()
    return list((await session.execute(q)).scalars().all())


async def visible_channel(session, ctx, channel_id: str) -> SitePublicChannel:
    """聊天一块用：当前查看者能看到的频道，看不到一律 404。"""
    c = await session.get(SitePublicChannel, channel_id)
    if c is None or not rating_visible(c.rating, ctx.effective_mode):
        raise not_found()
    return c


def _renumber(channels: list[SitePublicChannel]) -> None:
    for i, c in enumerate(channels):
        c.sort_order = i


def _require_manager(ctx):
    viewer = ctx.require_user()
    if not viewer.has_permission("channels.manage"):
        raise forbidden("你没有管理公共聊天区的权限")
    return viewer


def _read_input(raw, partial: bool) -> dict:
    data = raw if isinstance(raw, dict) else {}
    out: dict = {}
    errors: dict[str, str] = {}
    if "name" in data or not partial:
        name = data.get("name").strip() if isinstance(data.get("name"), str) else ""
        if not name or len(name) > 20:
            errors["name"] = "频道名 1 到 20 个字"
        else:
            out["name"] = name
    if data.get("description") is not None:
        desc = (
            data["description"].strip() if isinstance(data["description"], str) else ""
        )
        if len(desc) > 200:
            errors["description"] = "简介最多 200 字"
        else:
            out["description"] = desc
    if "rating" in data or not partial:
        if data.get("rating") not in RATINGS:
            errors["rating"] = "请选择分级"
        else:
            out["rating"] = data["rating"]
    if data.get("rules") is not None:
        rules = data["rules"].strip() if isinstance(data["rules"], str) else ""
        if len(rules) > 5000:
            errors["rules"] = "规则最多 5000 字"
        else:
            out["rules"] = rules
    if "fluxerChannelId" in data:
        value = data.get("fluxerChannelId")
        if value is not None and not (isinstance(value, str) and value.strip()):
            errors["fluxerChannelId"] = "要是频道 ID 或 null"
        else:
            out["fluxer_channel_id"] = value.strip() if isinstance(value, str) else None
    if errors:
        raise invalid(errors)
    return out


@router.get("/channels", summary="公共聊天区列表（全年龄模式下只给全年龄频道）")
async def list_channels(ctx: Ctx):
    async with get_session_factory()() as session:
        rows = await sorted_channels(session)
    return [
        channel_dto(c) for c in rows if rating_visible(c.rating, ctx.effective_mode)
    ]


@router.get("/admin/channels", summary="公共聊天区列表（站务后台，不按模式过滤）")
async def admin_channels(ctx: Ctx):
    _require_manager(ctx)
    async with get_session_factory()() as session:
        rows = await sorted_channels(session)
    return [channel_dto(c) for c in rows]


@router.post("/channels", status_code=201, summary="建频道（channels.manage）")
async def create_channel(ctx: Ctx, body: dict = Body(...)):
    viewer = _require_manager(ctx)
    fields = _read_input(body, False)
    raw_id = body.get("id")
    async with get_session_factory()() as session:
        channels = await sorted_channels(session, for_update=True)
        if raw_id not in (None, ""):
            channel_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if not CHANNEL_ID.match(channel_id):
                raise invalid({"id": "频道 ID 用 2 到 32 个小写字母、数字或连字符"})
            if await session.get(SitePublicChannel, channel_id) is not None:
                raise SiteError(
                    409, "conflict", "这个频道 ID 已经有了", {"field": "id"}
                )
        else:
            channel_id = "ch-" + secrets.token_hex(4)
        now = utc_now()
        channel = SitePublicChannel(
            id=channel_id,
            name=fields["name"],
            description=fields.get("description", ""),
            rating=fields["rating"],
            rules=fields.get("rules", ""),
            fluxer_channel_id=fields.get("fluxer_channel_id"),
            sort_order=len(channels),
            created_by=viewer.user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(channel)
        _renumber([*channels, channel])
        await session.commit()
        await session.refresh(channel)
        return channel_dto(channel)


@router.patch("/channels/{id}", summary="改频道、调整顺序（channels.manage）")
async def patch_channel(id: str, ctx: Ctx, body: dict = Body(...)):
    _require_manager(ctx)
    fields = _read_input(body, True)
    order = body.get("order") if isinstance(body, dict) else None
    if order is not None and (
        isinstance(order, bool) or not isinstance(order, int) or order < 0
    ):
        raise invalid({"order": "位置要是从 0 开始的整数"})
    async with get_session_factory()() as session:
        channels = await sorted_channels(session, for_update=True)
        channel = next((c for c in channels if c.id == id), None)
        if channel is None:
            raise not_found()
        for key, value in fields.items():
            setattr(channel, key, value)
        channel.updated_at = utc_now()
        if order is not None:
            # 移到第 order 个位置（超出就放最后），其余频道顺延
            rest = [c for c in channels if c is not channel]
            rest.insert(min(order, len(rest)), channel)
            _renumber(rest)
        await session.commit()
        await session.refresh(channel)
        return channel_dto(channel)


@router.delete("/channels/{id}", status_code=204, summary="删频道（channels.manage）")
async def delete_channel(id: str, ctx: Ctx):
    _require_manager(ctx)
    async with get_session_factory()() as session:
        channels = await sorted_channels(session, for_update=True)
        channel = next((c for c in channels if c.id == id), None)
        if channel is None:
            raise not_found()
        await session.delete(channel)
        _renumber([c for c in channels if c is not channel])
        await session.commit()
    return Response(status_code=204)


async def channel_count(session) -> int:
    return int(
        (
            await session.execute(select(func.count()).select_from(SitePublicChannel))
        ).scalar_one()
    )
