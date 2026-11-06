"""作者页精选（pinnedWorks）和勋章墙（装饰身份组），GET /users/{id} 和 /me/* 共用。"""

from sqlalchemy import select

from models.site_me import (
    SiteDecorationRole,
    SitePinnedWorks,
    SiteUserDecorationGrant,
    SiteUserDecorationPref,
)
from models.work import Work
from shared.time_utils import utc_now
from site_v1.dto import iso_utc
from site_v1.services import social
from site_v1.services.works import listable_conditions, serialize_works

PINNED_WORKS_MAX = 3
PINNED_WORK_NOTE_MAX = 40
DECORATION_SHOWCASE_MAX = 3
DECORATION_SLOTS = ("nameColor", "frame", "font", "title")


async def pinned_works_of(session, ctx, author_id: str) -> list[dict]:
    """当前查看者能看到的精选（看不到的整项跳过，不留空位；屏蔽了作者时为空）。"""
    viewer = ctx.viewer
    if viewer is not None and author_id != viewer.user_id:
        if await social.is_blocked(session, viewer.user_id, author_id):
            return []
    row = await session.get(SitePinnedWorks, author_id)
    items = [i for i in (row.items if row else []) if isinstance(i, dict)]
    ids = [int(i["workId"]) for i in items if str(i.get("workId", "")).isdigit()]
    if not ids:
        return []
    works = {
        w.id: w
        for w in (
            await session.execute(
                select(Work).where(
                    Work.id.in_(ids),
                    Work.author_id == author_id,
                    *(await listable_conditions(session, ctx)),
                )
            )
        ).scalars()
    }
    ordered = [works[i] for i in ids if i in works]
    dtos = {int(d["id"]): d for d in await serialize_works(session, ctx, ordered)}
    return [
        {
            "workId": str(i["workId"]),
            "note": i.get("note") or "",
            "work": dtos[int(i["workId"])],
        }
        for i in items
        if str(i.get("workId", "")).isdigit() and int(i["workId"]) in dtos
    ]


# ---------------- 勋章墙 ----------------


async def _owned(
    session, user_id: str
) -> list[tuple[SiteDecorationRole, SiteUserDecorationGrant]]:
    rows = (
        await session.execute(
            select(SiteDecorationRole, SiteUserDecorationGrant)
            .join(
                SiteUserDecorationGrant,
                SiteUserDecorationGrant.role_id == SiteDecorationRole.id,
            )
            .where(SiteUserDecorationGrant.user_id == user_id)
            .order_by(SiteDecorationRole.sort.desc(), SiteDecorationRole.id)
        )
    ).all()
    return [(r, g) for r, g in rows]


def _resolved_slots(
    owned, pref: SiteUserDecorationPref | None
) -> dict[str, str | None]:
    """每个槽位生效的组：自选过的按自选（null = 不用），没选过的取排序最高、能提供这个槽位的组。"""
    chosen = (pref.equipped if pref else None) or {}
    slots: dict[str, str | None] = {}
    for slot in DECORATION_SLOTS:
        if slot in chosen:
            rid = chosen[slot]
            slots[slot] = (
                rid
                if any(r.id == rid and slot in (r.slots or []) for r, _ in owned)
                else None
            )
        else:
            slots[slot] = next(
                (r.id for r, _ in owned if slot in (r.slots or [])), None
            )
    return slots


async def my_decorations(session, user_id: str) -> dict:
    owned = await _owned(session, user_id)
    pref = await session.get(SiteUserDecorationPref, user_id)
    slots = _resolved_slots(owned, pref)
    roles = []
    for r, g in owned:
        equipped_slots = [s for s in DECORATION_SLOTS if slots[s] == r.id]
        roles.append(
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "icon": r.icon,
                "color": r.color,
                "rarity": r.rarity or "common",
                "grantedAt": iso_utc(g.granted_at),
                "source": g.source,
                "equipped": bool(equipped_slots),
                "slots": list(r.slots or []),
                "equippedSlots": equipped_slots,
            }
        )
    by_id = {r.id: r for r, _ in owned}
    showcase = [i for i in ((pref.showcase if pref else None) or []) if i in by_id]
    name_role = by_id.get(slots["nameColor"]) if slots["nameColor"] else None
    frame_role = by_id.get(slots["frame"]) if slots["frame"] else None
    return {
        "decorationRoles": roles,
        "showcase": showcase,
        "showcaseHidden": bool(pref.showcase_hidden) if pref else False,
        "decorations": {
            "nameColor": name_role.color if name_role else None,
            "font": slots["font"],
            "frame": frame_role.color if frame_role else None,
            "badges": showcase,
            "title": by_id[slots["title"]].name if slots["title"] in by_id else None,
        },
    }


async def grant_decoration(
    session, user_id: str, role_id: str, source: str, fallback_name: str
) -> bool:
    """发一个装饰身份组（勋章）给用户，GET /me/decorations、GET /users/{id} 里就能看到。

    - 身份组不存在时按 fallback_name 建一个最简单的（同假后端 grantDecoration）；
    - 已经有了不重复发，返回 False；只 flush，不 commit。
    """
    role = await session.get(SiteDecorationRole, role_id)
    if role is None:
        session.add(
            SiteDecorationRole(
                id=role_id,
                name=fallback_name[:60] or role_id,
                description=None,
                icon=None,
                color="#f59e0b",
                rarity="rare",
                slots=[],
                sort=0,
            )
        )
        await session.flush()
    if await session.get(SiteUserDecorationGrant, (user_id, role_id)) is not None:
        return False
    session.add(
        SiteUserDecorationGrant(
            user_id=user_id, role_id=role_id, granted_at=utc_now(), source=source[:200]
        )
    )
    await session.flush()
    return True
