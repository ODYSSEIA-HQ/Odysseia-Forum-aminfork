"""协议 1.4 分页：?cursor=&limit=，响应 {items, nextCursor}。

- limit 默认 20，上限 100，超过按 100（不报 422）；小于 1 或不是数字按默认值。
- 游标对前端不透明：base64url(JSON)。两种用法：
  * offset 游标：PageRequest.offset，适合排序稳定、数据量不大的列表；
  * keyset 游标：PageRequest.after 存上一页最后一行的排序键（例如 [created_at_iso, id]），
    排序不稳定或数据会插入的列表应该用它（避免翻页重复、漏项）。
- 坏游标按 422 validation_failed（fields.cursor）。

用法：
    page = page_request(cursor, limit)              # 路由里拿到参数
    rows = ... .offset(page.offset).limit(page.limit + 1)   # 多取一条判断有没有下一页
    return page_response(rows, page, serialize)     # 自动截断并生成 nextCursor

    # keyset：
    rows = ... .where(tuple_(col, id) < page.after) ... .limit(page.limit + 1)
    return page_response(rows, page, serialize, key=lambda r: [iso_utc(r.created_at), r.id])
"""

import base64
import json
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from fastapi import Query

from site_v1.errors import invalid

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True)
class PageRequest:
    limit: int
    offset: int = 0
    after: list | None = None
    """keyset 游标：上一页最后一行的排序键；第一页为 None。"""


def clamp_limit(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if value < 1:
        return DEFAULT_LIMIT
    return min(value, MAX_LIMIT)


def encode_cursor(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        raise invalid({"cursor": "游标无效，请从第一页重新开始"})


def page_request(cursor: str | None, limit: Any) -> PageRequest:
    size = clamp_limit(limit if limit is not None else DEFAULT_LIMIT)
    if not cursor:
        return PageRequest(limit=size)
    data = decode_cursor(cursor)
    offset = data.get("o", 0)
    after = data.get("k")
    if (
        not isinstance(offset, int)
        or offset < 0
        or (after is not None and not isinstance(after, list))
    ):
        raise invalid({"cursor": "游标无效，请从第一页重新开始"})
    return PageRequest(limit=size, offset=offset, after=after)


def pagination(
    cursor: str | None = Query(None, description="上一页响应的 nextCursor"),
    limit: str | None = Query(None, description="每页条数，默认 20，上限 100"),
) -> PageRequest:
    """FastAPI 依赖：limit 用字符串接收，超过 100 按 100，不报 422。"""
    return page_request(cursor, limit)


def page_response(
    rows: Sequence[Any],
    page: PageRequest,
    serialize: Callable[[Any], Any],
    *,
    key: Callable[[Any], list] | None = None,
) -> dict:
    """rows 应该多取一条（limit+1）；返回 {items, nextCursor}。

    key 给了就用 keyset 游标，否则用 offset 游标。
    """
    has_more = len(rows) > page.limit
    rows = list(rows[: page.limit])
    next_cursor = None
    if has_more and rows:
        if key is not None:
            next_cursor = encode_cursor({"k": key(rows[-1])})
        else:
            next_cursor = encode_cursor({"o": page.offset + len(rows)})
    return {"items": [serialize(r) for r in rows], "nextCursor": next_cursor}


def empty_page() -> dict:
    return {"items": [], "nextCursor": None}
