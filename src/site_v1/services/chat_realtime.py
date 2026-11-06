"""聊天面板的实时推送（Centrifugo）和轮询用的「改动记录」。

面板事件（前端 ChatChannelEvent，带 target）：
- message.created：发消息；
- message.updated：编辑、删除（整条消息，deletedState 有值、body 为空串、附件和表情清空）、表情变化；
- typing：POST .../typing。
频道：作品交流区 chat:work.{id}、公共聊天区 chat:channel.{id}；私信不单独订阅，推到双方的 personal:{userId}。

交流区 / 公共聊天区是一个频道发给所有订阅者，消息按「这个频道最低的可见范围」序列化：
作品受限 → 按 r18 模式，否则按全年龄；公共频道按频道分级；都不开 R18G（附件给占位）。
reactions.me 是每个人不同的，频道广播里一律为 false（自己点的表情以接口响应为准）。
私信按每个收件人自己的有效模式分别序列化。

另外发 unread.updated（外层 4.3）：私信对方的会话未读数（scope=conv）、自己标记已读后的未读数。

轮询（GET .../messages?updatedAfter=）：编辑、删除、表情变化记进 Redis 有序集合
site:chat:changes:{fluxer 频道}（分数 = 毫秒时间戳，留最近 CHANGE_KEEP 条、一天），
带 updatedAfter 时响应多给 updates（这之后改过的消息），见 chat_messages.list_messages。

全部在事务提交之后调用；推送失败只记日志。
"""

import logging
import time

from site_v1 import mode as modes
from site_v1.context import SiteContext
from site_v1.realtime import channels, publisher
from site_v1.realtime.config import realtime_config
from site_v1.realtime.events import outer_later, viewer_ctx

logger = logging.getLogger(__name__)

CHANGE_KEEP = 200
CHANGE_TTL = 86400
UNREAD_SCOPE = {"work": "chat", "dm": "conv", "channel": "channel"}


def _changes_key(channel_id: str) -> str:
    return f"site:chat:changes:{channel_id}"


async def record_change(channel_id: str, message_id: str) -> None:
    """编辑、删除、表情：记一笔，给轮询的 updatedAfter 用。和推送是否开启无关。"""
    from site_v1.deps import get_redis

    try:
        redis = get_redis()
        key = _changes_key(channel_id)
        await redis.zadd(key, {message_id: int(time.time() * 1000)})
        await redis.zremrangebyrank(key, 0, -(CHANGE_KEEP + 1))
        await redis.expire(key, CHANGE_TTL)
    except Exception:  # noqa: BLE001
        logger.info("记录聊天改动失败：%s", channel_id)


async def changed_since(channel_id: str, since_ms: int, limit: int) -> list[str]:
    """since_ms 及之后改过的消息 ID（按改动时间正序，最多 limit 条，取最新的）。"""
    from site_v1.deps import get_redis

    try:
        raw = await get_redis().zrangebyscore(
            _changes_key(channel_id), since_ms, "+inf"
        )
    except Exception:  # noqa: BLE001
        return []
    ids = [x.decode() if isinstance(x, bytes) else str(x) for x in raw]
    return ids[-limit:]


async def broadcast_ctx(session, chan) -> SiteContext:
    """频道广播用的上下文：这个频道所有订阅者都满足的最低可见范围。"""
    from site_v1.services.work_access import restricted_ids, work_rating

    ctx = SiteContext()
    if chan.work is not None:
        rating = work_rating(chan.work)
        restricted = bool(await restricted_ids(session, [chan.work.id]))
        if modes.work_visibility(rating, restricted) == "restricted":
            ctx.effective_mode = modes.R18
    elif chan.public is not None and chan.public.rating != modes.GENERAL:
        ctx.effective_mode = modes.R18
    return ctx


def _enabled() -> bool:
    return realtime_config().publish_enabled


async def push_message(
    session, ctx, chan, message, event_type: str, own_dto: dict | None = None
) -> None:
    """message.created / message.updated。own_dto：已经按当前查看者序列化好的（接口响应）。"""
    if not _enabled():
        return
    from site_v1.services.chat_messages import to_dtos

    try:
        if chan.kind == "dm":
            viewer_id = ctx.viewer.user_id if ctx.viewer else None
            for user_id in dict.fromkeys(u for u in (viewer_id, chan.peer_id) if u):
                if user_id == viewer_id and own_dto is not None:
                    dto = own_dto
                else:
                    rctx = ctx if user_id == viewer_id else await viewer_ctx(user_id)
                    if rctx is None:
                        continue
                    dto = (await to_dtos(session, rctx, chan, [message]))[0]
                publisher.publish_later(
                    channels.personal(user_id),
                    {"type": event_type, "target": chan.target, "message": dto},
                )
            return
        dto = (
            await to_dtos(session, await broadcast_ctx(session, chan), chan, [message])
        )[0]
        # 发送者据 nonce 去重（前端 timeline.mergeMessages），频道广播也带上
        dto["nonce"] = message.nonce or None
        publisher.publish_later(
            channels.chat(chan.kind, chan.id),
            {"type": event_type, "target": chan.target, "message": dto},
        )
    except Exception:  # noqa: BLE001
        logger.exception("推送聊天消息失败：%s", chan.key)


def push_typing(ctx, chan) -> None:
    if not _enabled() or ctx.viewer is None:
        return
    data = {"type": "typing", "target": chan.target, "userId": ctx.viewer.user_id}
    if chan.kind == "dm":
        if chan.peer_id:
            publisher.publish_later(channels.personal(chan.peer_id), data)
        return
    publisher.publish_later(channels.chat(chan.kind, chan.id), data)


def push_unread(chan, user_id: str, count: int) -> None:
    if not _enabled():
        return
    outer_later(
        channels.personal(user_id),
        "unread.updated",
        {"scope": UNREAD_SCOPE[chan.kind], "id": chan.id, "count": max(0, int(count))},
    )


async def push_peer_unread(session, ctx, chan) -> None:
    """私信发出后，对方的会话未读数（用发送者的 Fluxer 令牌读频道，按对方的已读位置数）。"""
    if not _enabled() or chan.kind != "dm" or not chan.peer_id:
        return
    from site_v1.services.chat_messages import unread_of

    try:
        _last, count, _mentions = await unread_of(session, ctx, chan, chan.peer_id)
    except Exception:  # noqa: BLE001
        logger.info("算私信对方未读数失败：%s", chan.key)
        return
    push_unread(chan, chan.peer_id, count)
