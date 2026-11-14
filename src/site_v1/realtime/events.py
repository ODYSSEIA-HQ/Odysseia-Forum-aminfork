"""外层事件（协议 4.3 信封）和面板事件的组装、发布。

外层事件：{type, id, ts, data}（realtime-events.schema.json）。
面板事件：{type: message.created | message.updated | typing, target, ...}（前端 ChatChannelEvent）。
删除按面板约定表示为 message.updated：整条消息，deletedState 有值、body 为空串。
"""

import logging
import uuid

from shared.time_utils import utc_now
from site_v1.context import SiteContext
from site_v1.dto import iso_utc
from site_v1.realtime import channels, publisher
from site_v1.realtime.config import realtime_config

logger = logging.getLogger(__name__)

TRACK_TTL = 86400


def envelope(event_type: str, data: dict) -> dict:
    return {
        "type": event_type,
        "id": f"ev_{uuid.uuid4().hex}",
        "ts": iso_utc(utc_now()),
        "data": data,
    }


def outer_later(channel: str, event_type: str, data: dict) -> None:
    """排一条外层事件（后台发布）。"""
    publisher.publish_later(channel, envelope(event_type, data))


async def outer(channel: str, event_type: str, data: dict) -> bool:
    return await publisher.publish(channel, envelope(event_type, data))


async def viewer_ctx(user_id: str) -> SiteContext | None:
    """按收件人当时的偏好和资格组一个上下文（没有 Fluxer 令牌，只用来判断可见性、序列化）。"""
    from site_v1.deps import get_session_factory
    from site_v1.services.accounts import load_viewer

    viewer = await load_viewer(get_session_factory(), user_id)
    if viewer is None:
        return None
    from site_v1.config import get_config

    if get_config().principal_mode:
        # 身份头模式：没有收件人的 Principal，用他最近一次请求的 mode（记不到按 general）
        from dataclasses import replace

        from site_v1 import principal as principals
        from site_v1.deps import get_redis

        mode, show_r18g = await principals.last_seen(get_redis(), user_id)
        viewer = replace(
            viewer, prefs=replace(viewer.prefs, content_mode=mode, show_r18g=show_r18g)
        )
        ctx = SiteContext(viewer=viewer)
        ctx.effective_mode = mode
        return ctx
    ctx = SiteContext(viewer=viewer)
    ctx.refresh_mode()
    return ctx


# ---------------- 订阅了作品页 / 交流区的人（踢出用） ----------------


def _track_key(channel: str) -> str:
    return f"site:rt:subs:{channel}"


async def track_subscriber(channel: str, user_id: str) -> None:
    """签发订阅令牌时记下谁订了作品页 / 交流区；作品转为受限、下架时据此 unsubscribe。"""
    if not realtime_config().publish_enabled or not channels.is_tracked(channel):
        return
    try:
        from site_v1.deps import get_redis

        redis = get_redis()
        key = _track_key(channel)
        await redis.sadd(key, user_id)
        await redis.expire(key, TRACK_TTL)
    except Exception:  # noqa: BLE001
        logger.info("记录订阅者失败：%s", channel)


async def kick_invisible(work_id: int) -> None:
    """作品对某些订阅者不可见了（转为受限、下架、删除）：把他们从 work:{id}、chat:work.{id} 踢出。

    匿名订阅者没有用户 ID，靠订阅令牌短有效期（CENTRIFUGO_SUB_TOKEN_TTL）和
    work.visibility.changed 事件（前端收到后关闭页面和面板）兜底。
    """
    from site_v1.deps import get_redis, get_session_factory
    from site_v1.services.moderation_common import visible_work

    names = [channels.work(work_id), channels.chat("work", work_id)]
    try:
        redis = get_redis()
        members: set[str] = set()
        for name in names:
            members |= set(await redis.smembers(_track_key(name)))
    except Exception:  # noqa: BLE001
        logger.info("读订阅者失败：work %s", work_id)
        return
    for raw in members:
        user_id = raw.decode() if isinstance(raw, bytes) else str(raw)
        ctx = await viewer_ctx(user_id)
        if ctx is not None:
            async with get_session_factory()() as session:
                if await visible_work(session, ctx, work_id) is not None:
                    continue
        for name in names:
            await publisher.unsubscribe(user_id, name)
            try:
                await redis.srem(_track_key(name), user_id)
            except Exception:  # noqa: BLE001
                pass
