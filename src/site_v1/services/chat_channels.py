"""公共聊天区频道（kind=channel）在聊天代转层的读取约定。

频道对象（GET/POST/PATCH/DELETE /channels）由频道领域做，这里只按约定读：
频道表有 id、name、rating（general/r18/r18g），以及一个可空的 fluxer_channel_id
（公共聊天区对应的 Fluxer 频道 ID）。

- 默认实现在已注册的 SQLModel 表里找带这几列的表（不依赖频道领域的模块名），
  有 deleted_at 列时只认没删的；找不到这样的表就当没有频道。
- 频道领域也可以直接调 set_public_channel_source(get, list) 注册自己的读取函数。
- fluxer_channel_id 为空：FLUXER_FAKE=1 时用假频道 channel-{id}；真实环境视为还没建好
  （canOpen=false，消息接口 404）。
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select

REQUIRED_COLUMNS = {"id", "name", "rating", "fluxer_channel_id"}


@dataclass(frozen=True)
class PublicChannelRef:
    id: str
    name: str
    rating: str
    fluxer_channel_id: str | None
    order: int = 0


GetFn = Callable[[object, str], Awaitable[PublicChannelRef | None]]
ListFn = Callable[[object], Awaitable[list[PublicChannelRef]]]

_get: GetFn | None = None
_list: ListFn | None = None


def set_public_channel_source(get: GetFn | None, list_: ListFn | None) -> None:
    """注册频道读取函数（频道领域或测试用）；传 None 恢复默认的按表自动查找。"""
    global _get, _list
    _get, _list = get, list_


def _find_table():
    import models  # noqa: F401  确保模型都注册到 metadata
    from sqlmodel import SQLModel

    candidates = [
        t
        for t in SQLModel.metadata.tables.values()
        if REQUIRED_COLUMNS <= set(t.c.keys())
    ]
    candidates.sort(key=lambda t: (0 if "channel" in t.name else 1, t.name))
    return candidates[0] if candidates else None


def _ref(row) -> PublicChannelRef:
    data = row._mapping
    fluxer = data.get("fluxer_channel_id")
    return PublicChannelRef(
        id=str(data["id"]),
        name=str(data["name"] or ""),
        rating=str(data["rating"] or "general"),
        fluxer_channel_id=str(fluxer) if fluxer else None,
        order=int(data.get("order") or data.get("sort_order") or 0),
    )


async def get_public_channel(session, channel_id: str) -> PublicChannelRef | None:
    if _get is not None:
        return await _get(session, channel_id)
    table = _find_table()
    if table is None or not channel_id or len(channel_id) > 64:
        return None
    id_col = table.c.id
    try:
        int_key = id_col.type.python_type is int
    except NotImplementedError:
        int_key = False
    if int_key and not channel_id.isdigit():
        return None
    q = select(table).where(id_col == (int(channel_id) if int_key else channel_id))
    if "deleted_at" in table.c:
        q = q.where(table.c.deleted_at.is_(None))
    row = (await session.execute(q)).first()
    return _ref(row) if row else None


async def list_public_channels(session) -> list[PublicChannelRef]:
    if _list is not None:
        return await _list(session)
    table = _find_table()
    if table is None:
        return []
    q = select(table)
    if "deleted_at" in table.c:
        q = q.where(table.c.deleted_at.is_(None))
    refs = [_ref(r) for r in (await session.execute(q)).all()]
    refs.sort(key=lambda c: (c.order, c.id))
    return refs


def fluxer_channel_of(channel: PublicChannelRef) -> str | None:
    if channel.fluxer_channel_id:
        return channel.fluxer_channel_id
    from site_v1.config import get_config

    return f"channel-{channel.id}" if get_config().fluxer_fake else None
