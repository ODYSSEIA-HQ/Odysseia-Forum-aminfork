"""在线状态（协议 0.5 可选能力 presence）。全部在 Redis：

- 「正在看」：site:presence:view:{kind}:{频道} 有序集合，成员 = 用户 ID，分数 = 最后心跳毫秒；
  60 秒没心跳算离开（读的时候按分数过滤，键本身 2 分钟过期）。隐身的人不写进去。
- 「正在输入」：site:presence:typing:{kind}:{频道}，同样的形状，8 秒内算正在输入；关了 showTyping 的不写。
- 在线：site:presence:online:{用户}，任一心跳续 60 秒。
- 偏好：site:presence:settings:{用户} 哈希（showOnline / showTyping / invisible，"1"/"0"），不过期。
"""

import time

from sqlalchemy import select

from models.site_account import SiteUserBlock
from site_v1.deps import get_redis
from site_v1.dto import load_users
from site_v1.errors import invalid
from site_v1.services import social

VIEW_WINDOW_MS = 60_000
TYPING_WINDOW_MS = 8_000
KEY_TTL = 120
MAX_VIEWERS = 50
DEFAULTS = {"showOnline": True, "showTyping": True, "invisible": False}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _view_key(chan) -> str:
    return f"site:presence:view:{chan.kind}:{chan.channel_id}"


def _typing_key(chan) -> str:
    return f"site:presence:typing:{chan.kind}:{chan.channel_id}"


def _online_key(user_id: str) -> str:
    return f"site:presence:online:{user_id}"


def _settings_key(user_id: str) -> str:
    return f"site:presence:settings:{user_id}"


def _s(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def get_settings(user_id: str) -> dict:
    raw = await get_redis().hgetall(_settings_key(user_id))
    data = {_s(k): _s(v) for k, v in (raw or {}).items()}
    return {k: (data[k] == "1") if k in data else d for k, d in DEFAULTS.items()}


async def put_settings(user_id: str, body) -> dict:
    if not isinstance(body, dict):
        raise invalid({"body": "要是对象"})
    errors = {k: "要是布尔值" for k in DEFAULTS if not isinstance(body.get(k), bool)}
    if errors:
        raise invalid(errors)
    values = {k: "1" if body[k] else "0" for k in DEFAULTS}
    redis = get_redis()
    await redis.hset(_settings_key(user_id), mapping=values)
    if body["invisible"] or not body["showOnline"]:
        await redis.delete(_online_key(user_id))
    return {k: body[k] for k in DEFAULTS}


async def heartbeat(chan, user_id: str) -> None:
    settings = await get_settings(user_id)
    redis = get_redis()
    if settings["invisible"]:
        await redis.zrem(_view_key(chan), user_id)
        return
    now = _now_ms()
    key = _view_key(chan)
    await redis.zadd(key, {user_id: now})
    await redis.zremrangebyscore(key, "-inf", now - VIEW_WINDOW_MS)
    await redis.expire(key, KEY_TTL)
    if settings["showOnline"]:
        await redis.set(_online_key(user_id), "1", ex=VIEW_WINDOW_MS // 1000)


async def leave(chan, user_id: str) -> None:
    redis = get_redis()
    await redis.zrem(_view_key(chan), user_id)
    await redis.zrem(_typing_key(chan), user_id)


async def note_typing(chan, user_id: str) -> bool:
    """记「正在输入」；返回是否允许别人看到（showTyping 且不隐身）。"""
    try:
        settings = await get_settings(user_id)
    except Exception:  # noqa: BLE001
        return True
    if settings["invisible"] or not settings["showTyping"]:
        return False
    if chan.kind == "dm":
        return True
    try:
        redis = get_redis()
        key = _typing_key(chan)
        await redis.zadd(key, {user_id: _now_ms()})
        await redis.expire(key, KEY_TTL)
    except Exception:  # noqa: BLE001
        pass
    return True


async def _blocked_either_way(
    session, viewer_id: str | None, ids: list[str]
) -> set[str]:
    if not viewer_id or not ids:
        return set()
    mine = await social.blocked_user_ids(session, viewer_id)
    theirs = set(
        (
            await session.execute(
                select(SiteUserBlock.user_id).where(
                    SiteUserBlock.blocked_user_id == viewer_id,
                    SiteUserBlock.user_id.in_(ids),
                )
            )
        ).scalars()
    )
    return (mine & set(ids)) | theirs


async def viewers(session, ctx, chan) -> dict:
    redis = get_redis()
    now = _now_ms()
    raw = await redis.zrangebyscore(_view_key(chan), now - VIEW_WINDOW_MS, "+inf")
    ids = [_s(x) for x in raw]
    typing_raw = await redis.zrangebyscore(
        _typing_key(chan), now - TYPING_WINDOW_MS, "+inf"
    )
    typing = {_s(x) for x in typing_raw}
    visible = []
    for uid in ids:
        if not (await get_settings(uid))["invisible"]:
            visible.append(uid)
    viewer_id = ctx.viewer.user_id if ctx.viewer else None
    hidden = await _blocked_either_way(session, viewer_id, visible)
    shown = [u for u in visible if u not in hidden]
    users = await load_users(session, shown)
    shown = [u for u in shown if u in users]
    shown.sort(key=lambda u: (u not in typing,))
    return {
        "count": len(shown),
        "items": [
            {"user": users[u], "typing": u in typing} for u in shown[:MAX_VIEWERS]
        ],
    }


async def user_online(session, ctx, user_id: str) -> bool:
    settings = await get_settings(user_id)
    if settings["invisible"] or not settings["showOnline"]:
        return False
    viewer_id = ctx.viewer.user_id if ctx.viewer else None
    if viewer_id and viewer_id != user_id:
        if await social.is_blocked(session, user_id, viewer_id):
            return False
    return bool(await get_redis().exists(_online_key(user_id)))
