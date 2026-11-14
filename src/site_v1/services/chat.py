"""聊天外层（协议 3.5）：未读汇总、聊天目标信息。

- 作品交流区 = 作品的 Fluxer 原生主帖（moderation_common.native_thread_id）。
- canOpen：作品对查看者可见（否则 404）且原生主帖已建好、Fluxer 认得这个频道。
  FLUXER_FAKE=1 时没有原生主帖的作品用假频道 ID（work-{id}），视为已建好，
  这样契约测试里刚发布的作品能打开（作品领域在 fake 模式不排 create_main）。
- 成员数、未读、@ 数来自 FluxerPort.chat_target_info / unread_summary。
- 作品内禁言（V2 work_user_mute）和全站禁言（user_moderation_record.kind=mute）
  反映在扩展字段 canPost / mutedUntil 上（协议 ChatTargetInfo 之外，前端可选用）。
"""

from datetime import UTC, datetime

from site_v1.config import get_config
from site_v1.errors import not_found, unauthenticated, unavailable
from site_v1.fluxer import FluxerUnavailable, get_fluxer
from site_v1.services.work_access import work_visible
from site_v1.services.moderation_common import (
    native_thread_id,
    parse_int_id,
    site_muted_until,
    visible_work,
    work_mute,
    works_by_native_thread,
)
from site_v1.dto import iso_utc, load_users
from models.work import Work
from site_v1.services.chat_channels import (
    fluxer_channel_of,
    get_public_channel,
    list_public_channels,
)

CHAT_KINDS = ("work", "dm", "channel")


def _count(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _iso_or_none(value) -> str | None:
    """Fluxer 给的时间：ISO 字符串或毫秒时间戳，统一成协议 IsoTime。"""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return iso_utc(datetime.fromtimestamp(value / 1000, UTC))
        text = str(value).replace("Z", "+00:00")
        return iso_utc(datetime.fromisoformat(text))
    except (ValueError, OverflowError, OSError):
        return None


async def unread_summary(session, ctx) -> list[dict]:
    viewer = ctx.require_user()
    try:
        summary = await get_fluxer().unread_summary(ctx.fluxer_token or "", viewer.user_id)
    except FluxerUnavailable as exc:
        raise unavailable("未读汇总暂时不可用") from exc
    thread_ids = [str(w.get("thread_id") or "") for w in summary.works]
    works = await works_by_native_thread(session, thread_ids)
    # FLUXER_FAKE=1：没有原生主帖的作品用假频道 work-{id}
    for thread in thread_ids:
        if thread.startswith("work-") and thread not in works:
            work = await session.get(Work, parse_int_id(thread[5:]) or 0)
            if work is not None:
                works[thread] = work
    items: list[dict] = []
    for entry in summary.works:
        work = works.get(str(entry.get("thread_id") or ""))
        # 受限作品在全年龄模式下不返回、不计入；下架、删除的作品同样不返回
        if not await work_visible(session, ctx, work):
            continue
        items.append(
            {
                "target": {"kind": "work", "id": str(work.id)},
                "count": _count(entry.get("unread")),
                "mentions": _count(entry.get("mentions")),
                "lastMessageAt": _iso_or_none(entry.get("last_message_at")),
            }
        )
    # 公共聊天区：当前模式下能看到的都返回（侧栏要列出来），没进过的未读是 0
    by_channel = {str(c.get("channel_id") or ""): c for c in summary.channels}
    for channel in await list_public_channels(session):
        if channel.rating not in ctx.visible_ratings():
            continue
        entry = by_channel.get(fluxer_channel_of(channel) or "") or {}
        items.append(
            {
                "target": {"kind": "channel", "id": channel.id},
                "count": _count(entry.get("unread")),
                "mentions": _count(entry.get("mentions")),
                "lastMessageAt": _iso_or_none(entry.get("last_message_at")),
            }
        )
    for entry in summary.dms:
        channel = str(entry.get("channel_id") or "")
        if not channel:
            continue
        items.append(
            {
                "target": {"kind": "dm", "id": channel},
                "count": _count(entry.get("unread")),
                "mentions": _count(entry.get("mentions")),
                "lastMessageAt": _iso_or_none(entry.get("last_message_at")),
            }
        )
    return items


async def _channel_info(ctx, kind: str, target_id: str) -> dict | None:
    try:
        return await get_fluxer().chat_target_info(
            ctx.fluxer_token,
            kind,
            target_id,
            viewer_id=ctx.viewer.user_id if ctx.viewer else None,
        )
    except FluxerUnavailable as exc:
        raise unavailable("聊天服务暂时不可用") from exc


async def target_info(session, ctx, kind: str, raw_id: str) -> dict:
    if kind not in CHAT_KINDS:
        raise not_found()
    if kind == "dm":
        viewer = ctx.viewer
        if viewer is None:
            raise unauthenticated()
        info = await _channel_info(ctx, "dm", raw_id)
        # Fluxer 只对会话成员返回频道信息；不是成员、不存在都按 404
        if not info:
            raise not_found()
        peer_id = str(info.get("peer_user_id") or "")
        title = info.get("title") or ""
        if peer_id:
            peer = (await load_users(session, [peer_id])).get(peer_id)
            if peer:
                title = peer["displayName"]
        site_until = await site_muted_until(session, viewer.user_id)
        return {
            "target": {"kind": "dm", "id": raw_id},
            "canOpen": True,
            "title": title or "私信",
            "memberCount": _count(info.get("member_count")) or 2,
            "canPost": site_until is None,
            "mutedUntil": iso_utc(site_until),
        }

    if kind == "channel":
        channel = await get_public_channel(session, raw_id)
        if channel is None or channel.rating not in ctx.visible_ratings():
            raise not_found()
        fluxer_id = fluxer_channel_of(channel)
        info = await _channel_info(ctx, "channel", fluxer_id) if fluxer_id else None
        viewer = ctx.viewer
        site_until = await site_muted_until(session, viewer.user_id) if viewer else None
        return {
            "target": {"kind": "channel", "id": channel.id},
            "canOpen": bool(info),
            "title": channel.name,
            "memberCount": _count(info.get("member_count")) if info else 0,
            "canPost": bool(info) and viewer is not None and site_until is None,
            "mutedUntil": iso_utc(site_until),
        }

    work = await visible_work(session, ctx, raw_id)
    if work is None:
        raise not_found()
    thread = await native_thread_id(session, work)
    if thread is None and get_config().fluxer_fake:
        thread = f"work-{work.id}"
    info = await _channel_info(ctx, "work", thread) if thread else None
    can_open = bool(info)
    result = {
        "target": {"kind": "work", "id": str(work.id)},
        "canOpen": can_open,
        "title": work.title,
        "memberCount": _count(info.get("member_count")) if info else 0,
        "canPost": False,
        "mutedUntil": None,
    }
    viewer = ctx.viewer
    if viewer is not None and can_open:
        muted, work_until = await work_mute(session, work.id, viewer.user_id)
        site_until = await site_muted_until(session, viewer.user_id)
        result["canPost"] = not muted and site_until is None
        # 永久的作品内禁言 mutedUntil 为 null、canPost=false
        deadlines = [d for d in (work_until, site_until) if d is not None]
        if not (muted and work_until is None) and deadlines:
            result["mutedUntil"] = iso_utc(max(deadlines))
    return result
