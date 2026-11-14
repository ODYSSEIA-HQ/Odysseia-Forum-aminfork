"""身份头模式下的本地用户行（网关与核心接入约定第 3 节）。

- 用户 ID 就是网关（账号模块）发的 ID，原样作为 user.id，不做映射。
- 第一次见到某个 userId 时幂等建一行 user（INSERT … ON CONFLICT DO NOTHING，同 identity/auth_runtime
  的投影写法）和 site_account / prefs / restricted_access（services.accounts.ensure_account_rows）。
- 资料（用户名、显示名、头像）从网关 POST /internal/users/batch {ids} → {items: UserBrief[]} 拉，
  请求头 x-ody-internal-token。网关暂时不可用时先用占位资料（username = ID）建行，之后按节流重拉。
"""

import logging

import httpx
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from models.user import User
from site_v1.config import SiteConfig, get_config

logger = logging.getLogger(__name__)

TIMEOUT = 5.0
REFRESH_THROTTLE_PREFIX = "site:principal:refresh:"
REFRESH_THROTTLE_SECONDS = 60

_fetcher = None


def set_fetcher(fetcher) -> None:
    """测试用：替换「按 ID 批量拉资料」的实现（async (ids) -> list[dict]）。"""
    global _fetcher
    _fetcher = fetcher


async def fetch_briefs(ids: list[str], config: SiteConfig | None = None) -> list[dict]:
    """POST {gateway}/internal/users/batch；没配地址返回空（调用方按占位处理）。"""
    if _fetcher is not None:
        return await _fetcher(ids)
    config = config or get_config()
    if not config.gateway_internal_url or not ids:
        return []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            config.gateway_internal_url + "/internal/users/batch",
            json={"ids": ids},
            headers={"x-ody-internal-token": config.gateway_internal_token},
        )
        res.raise_for_status()
        items = res.json().get("items") or []
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)]


def _values(user_id: str, brief: dict | None) -> dict:
    brief = brief or {}
    username = brief.get("username")
    display = brief.get("displayName")
    avatar = brief.get("avatarUrl")
    return {
        "id": user_id,
        "username": username if isinstance(username, str) and username else user_id,
        "display_name": display if isinstance(display, str) and display else None,
        "avatar_url": avatar if isinstance(avatar, str) and avatar else None,
    }


async def _briefs_or_empty(ids: list[str]) -> dict[str, dict]:
    try:
        return {b["id"]: b for b in await fetch_briefs(ids)}
    except Exception:  # noqa: BLE001
        logger.warning("从网关拉用户资料失败，先用占位资料", exc_info=True)
        return {}


async def ensure_users(session_factory, ids, *, trusted: bool = False) -> None:
    """幂等：本地没有的 user 行按网关资料建上（含 site_account 等行）。已有的不动。

    trusted=True：ID 来自网关签名的 Principal（一定存在），网关资料拉不到时用占位资料建行；
    否则（例如私信对方的 ID 是用户填的）只建网关确认存在的。
    """
    from site_v1.services.accounts import ensure_account_rows

    wanted = sorted({i for i in ids if isinstance(i, str) and i and len(i) <= 64})
    if not wanted:
        return
    async with session_factory() as session:
        existing = set(
            (
                await session.execute(select(User.id).where(User.id.in_(wanted)))
            ).scalars()
        )
        missing = [i for i in wanted if i not in existing]
        if not missing:
            return
        briefs = await _briefs_or_empty(missing)
        for user_id in missing:
            if briefs.get(user_id, {}).get("deleted"):
                continue
            if user_id not in briefs and not trusted:
                continue
            await session.execute(
                insert(User)
                .values(**_values(user_id, briefs.get(user_id)))
                .on_conflict_do_nothing(index_elements=[User.id])
            )
            await ensure_account_rows(
                session,
                user_id,
                newcomer=bool(briefs.get(user_id, {}).get("isNewcomer")),
            )
        await session.commit()


async def refresh_placeholder(session_factory, redis, user_id: str) -> None:
    """占位资料（username = ID）的行：节流重拉一次网关资料。"""
    try:
        if not await redis.set(
            REFRESH_THROTTLE_PREFIX + user_id, "1", ex=REFRESH_THROTTLE_SECONDS, nx=True
        ):
            return
    except Exception:  # noqa: BLE001
        return
    brief = (await _briefs_or_empty([user_id])).get(user_id)
    if not brief:
        return
    values = _values(user_id, brief)
    async with session_factory() as session:
        stmt = insert(User).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[User.id],
                set_={k: v for k, v in values.items() if k != "id"},
                where=or_(
                    User.username.is_distinct_from(stmt.excluded.username),
                    User.display_name.is_distinct_from(stmt.excluded.display_name),
                    User.avatar_url.is_distinct_from(stmt.excluded.avatar_url),
                ),
            )
        )
        await session.commit()
