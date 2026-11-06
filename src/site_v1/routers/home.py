"""找作品的入口（草案 A、B、C 和前端草案）：
- GET /home（栏位 + 作品横幅 + 精选轮播 + 网格推广）、GET /home/featured、GET /home/promotions；
- GET /boards/overview（tag 排行榜页）、GET /search/suggest；
- 作品横幅：GET /banners、GET/POST /banners/applications、GET /admin/banners/applications、
  POST /admin/banners/applications/{id}/resolve（banners.review）；
- 首页配置：GET/PUT /admin/home、GET/PUT /admin/home/featured（home.manage）；
- 旧帖跳转：GET /works/by-legacy/discord/{threadId}。

形状和规则以假后端为准（src/mock/handlers/discovery.ts、featured.ts、homeAdmin.ts、dcArchive.ts）。
所有作品都按 services/works.listable_conditions 过滤（全年龄模式下受限作品不出现、不计数、不占位；
屏蔽 tag/用户过滤；r18g 按 showR18G），过滤后变空的首页栏位不返回。
要注册在 routers/tags.py 之前（/boards/overview 优先于 /boards/{id}）。

存储：栏位配置在 site_setting[home.rails]，精选和推广位在 site_setting[home.featured]。
横幅申请和 V2 共用 banner_application（target_type=3、work_id），协议多出的横幅图、备注、展示期在
site_banner_meta；全站横幅通过时同时写 banner_carousel（V2 首页也能看到，/banners 的全站横幅也从这里读，
所以 V2 审核通过的作品横幅也会出现）；tag 主榜横幅只在 site_banner_meta 记展示期。
"""

import math
import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select

from models.attachment import Attachment
from models.banner_application import BannerApplication
from models.banner_carousel import BannerCarousel
from models.site_account import SiteSetting
from models.site_nav import SiteBannerMeta
from models.site_tag import SiteTag, SiteWorkTag
from models.thread import Thread
from models.user import User
from models.work import Work
from models.work_statistics import WorkStatistics
from shared.time_utils import utc_now
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import iso_utc, load_users
from site_v1.errors import SiteError, forbidden, invalid, not_found
from site_v1.paging import PageRequest, page_response, pagination
from site_v1.routers.tags import BOARD_GROUPS, _boards
from site_v1.services import tag_directory as td
from site_v1.services import works as svc
from site_v1.services.attachments import attachment_dto, load_attachments
from site_v1.services.work_access import parse_id, work_rating, work_visible

router = APIRouter(tags=["首页与榜单"])

DAY = timedelta(days=1)
RAIL_LIMIT = 12
MAX_RAILS = 20
RAIL_KINDS = ("board", "list", "dc_classic", "curated", "event")
RAIL_SORTS = (
    "hot_day",
    "hot_week",
    "hot_month",
    "new",
    "rising",
    "hall",
    "latest",
    "rookie",
)
DEFAULT_RAILS = [
    {
        "kind": "board",
        "refId": "all",
        "title": "周热门",
        "order": 0,
        "sort": "hot_week",
    },
    {"kind": "board", "refId": "all", "title": "新作", "order": 1, "sort": "new"},
    {
        "kind": "board",
        "refId": "all",
        "title": "上升最快",
        "order": 2,
        "sort": "rising",
    },
    {"kind": "board", "refId": "all", "title": "殿堂", "order": 3, "sort": "hall"},
    {"kind": "dc_classic", "refId": None, "title": "DC 经典", "order": 4},
]

# 精选轮播和推广位（前端草案 featured.ts）
FEATURED_LIMIT = 12
PROMOTION_EVERY = 4
SOURCE_TAKE = {"hot_week": 4, "rising": 3, "new": 3, "editor": 3}
GRID_PROMOTION_LIMIT = 12
MAX_PROMOTIONS = 20
MAX_EDITOR_PICKS = 12
DEFAULT_WEIGHT = 5
OVERVIEW_TOP = 3


def _backend_sort(sort: str | None) -> str:
    """rookie（新人榜）后端还没有，先按周热门排。"""
    return sort if sort in svc.WORK_SORTS else "hot_week"


def _parse_time(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _exclude(request: Request) -> list[str]:
    return svc.parse_csv(request.query_params.getlist("exclude"))


async def _setting(session, key: str, default: dict) -> dict:
    row = await session.get(SiteSetting, key)
    return dict(row.value) if row and isinstance(row.value, dict) else dict(default)


async def _save_setting(session, key: str, value: dict) -> None:
    from sqlalchemy.dialects.postgresql import insert

    await session.execute(
        insert(SiteSetting)
        .values(key=key, value=value, updated_at=utc_now())
        .on_conflict_do_update(
            index_elements=[SiteSetting.key],
            set_={"value": value, "updated_at": utc_now()},
        )
    )


async def _ranked_ids(session, conds: list, sort: str, limit: int) -> list[int]:
    """按 sort 排好的作品 ID（不序列化）。"""
    q = (
        select(Work.id)
        .outerjoin(WorkStatistics, WorkStatistics.work_id == Work.id)
        .where(*conds)
    )
    if sort == "new":
        q = q.order_by(
            func.coalesce(Work.published_at, Work.created_at).desc(), Work.id.desc()
        )
    elif sort == "latest":
        q = q.order_by(Work.updated_at.desc(), Work.id.desc())
    elif sort == "hall":
        q = q.where(
            func.coalesce(Work.published_at, Work.created_at)
            <= utc_now() - timedelta(days=svc.HALL_MIN_DAYS)
        ).order_by(
            func.coalesce(WorkStatistics.collection_count, 0).desc(), Work.id.desc()
        )
    elif sort == "dc_classic":
        q = q.where(Work.source_platform == "discord").order_by(
            func.coalesce(WorkStatistics.reaction_count, 0).desc(), Work.id.desc()
        )
    else:
        q = q.order_by(svc.heat_score(sort).desc(), Work.id.desc())
    return list((await session.execute(q.limit(limit))).scalars().all())


async def _serialize_ids(session, ctx, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    rows = (await session.execute(select(Work).where(Work.id.in_(ids)))).scalars().all()
    by_id = {w.id: w for w in rows}
    works = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    return {int(d["id"]): d for d in await svc.serialize_works(session, ctx, works)}


# ---------------- 首页栏位 ----------------


async def _board_tag(session, board_id) -> int | None | bool:
    """主榜 ID → tag ID；all 为 None；不存在为 False。"""
    if board_id == "all":
        return None
    tag_id = parse_id(board_id)
    if tag_id is None:
        return False
    row = await session.get(SiteTag, tag_id)
    if row is None or row.deleted_at is not None or row.group_id not in BOARD_GROUPS:
        return False
    return tag_id


async def _rails_config(session) -> list[dict]:
    data = await _setting(session, "home.rails", {"rails": DEFAULT_RAILS})
    rails = data.get("rails") if isinstance(data.get("rails"), list) else DEFAULT_RAILS
    return sorted(rails, key=lambda r: r.get("order", 0))


async def home_rails(session, ctx) -> list[dict]:
    conds = await svc.listable_conditions(session, ctx)
    plans = []
    used: set[str] = set()
    for i, cfg in enumerate(await _rails_config(session)):
        kind = cfg.get("kind")
        ref = cfg.get("refId")
        sort = cfg.get("sort") or "hot_week"
        if kind == "board":
            base = sort if ref == "all" else f"{ref}:{sort}"
        elif kind == "dc_classic":
            base = "dc_classic"
        else:
            base = f"{kind}_{ref if ref is not None else i}"
        rail_id = f"{base}_{i}" if base in used else base
        used.add(rail_id)
        ids: list[int] = []
        rail = {
            "id": rail_id,
            "title": cfg.get("title") or "",
            "kind": kind,
            "moreLink": None,
        }
        if kind == "board" and ref:
            tag = await _board_tag(session, ref)
            rail.update(boardId=ref, sort=sort, moreLink=f"/boards/{ref}?sort={sort}")
            if tag is not False:
                extra = [svc.has_tag(tag)] if tag is not None else []
                ids = await _ranked_ids(
                    session, [*conds, *extra], _backend_sort(sort), RAIL_LIMIT
                )
        elif kind == "dc_classic":
            ids = await _ranked_ids(session, conds, "dc_classic", RAIL_LIMIT)
        elif kind == "curated":
            wanted = [parse_id(x) for x in cfg.get("workIds") or []]
            wanted = [x for x in wanted if x is not None]
            if wanted:
                ok = set(
                    (
                        await session.execute(
                            select(Work.id).where(Work.id.in_(wanted), *conds)
                        )
                    ).scalars()
                )
                ids = [x for x in wanted if x in ok][:RAIL_LIMIT]
        else:
            # 自定义榜单（list）和活动（event）的栏位：还没有这两种数据，先给空（过滤掉）
            if kind == "list":
                rail["listId"] = ref
            elif kind == "event":
                rail["eventId"] = ref
        plans.append((rail, ids))
    serialized = await _serialize_ids(
        session, ctx, [i for _, ids in plans for i in ids]
    )
    out = []
    for rail, ids in plans:
        rail["items"] = [serialized[i] for i in ids if i in serialized]
        if rail["items"]:
            out.append(rail)
    return out


# ---------------- 精选轮播、网格推广 ----------------


def promotion_id(p: dict) -> str:
    return f"{p.get('kind')}:{p.get('targetId')}"


def promotion_state(p: dict, now: datetime) -> str:
    start, end = _parse_time(p.get("startAt")), _parse_time(p.get("endAt"))
    if start is None or end is None:
        return "expired"
    if now < start:
        return "scheduled"
    return "active" if now < end else "expired"


def weighted_order(items: list, weight) -> list:
    """加权随机排序（不放回，Efraimidis–Spirakis）：权重高的更容易靠前，每条最多一次。"""
    keyed = [
        (math.pow(max(random.random(), 1e-9), 1 / max(1, weight(x))), x) for x in items
    ]
    keyed.sort(key=lambda k: k[0], reverse=True)
    return [x for _, x in keyed]


async def _featured_store(session) -> dict:
    data = await _setting(
        session, "home.featured", {"editorPicks": [], "promotions": []}
    )
    data.setdefault("editorPicks", [])
    data.setdefault("promotions", [])
    return data


async def _active_promotions(session, where: str, exclude: list[str]) -> list[dict]:
    now = utc_now()
    store = await _featured_store(session)
    items = [
        p
        for p in store["promotions"]
        if p.get("placement") in (where, "both")
        and promotion_state(p, now) == "active"
        and promotion_id(p) not in exclude
    ]
    return weighted_order(items, lambda p: p.get("weight") or DEFAULT_WEIGHT)


async def _type_conds(
    session, ctx, type_name: str | None, board: str | None = None
) -> list:
    conds = await svc.listable_conditions(session, ctx, type_name=type_name)
    if board and board != "all":
        tag = parse_id(board)
        from sqlalchemy import false

        conds.append(svc.has_tag(tag) if tag is not None else false())
    return conds


async def featured_items(
    session, ctx, type_name: str | None, exclude: list[str]
) -> list[dict]:
    conds = await _type_conds(session, ctx, type_name)
    store = await _featured_store(session)

    # 推广：作品类、对查看者可见、符合类型；同一作品只取一次
    promo_candidates = await _active_promotions(session, "carousel", exclude)
    promo_ids = [
        parse_id(p.get("targetId")) for p in promo_candidates if p.get("kind") == "work"
    ]
    editor_ids = [parse_id(x) for x in store["editorPicks"]]
    check = [x for x in {*promo_ids, *editor_ids} if x is not None]
    visible = set()
    if check:
        visible = set(
            (
                await session.execute(select(Work.id).where(Work.id.in_(check), *conds))
            ).scalars()
        )
    promoted: list[tuple[int, str]] = []
    for p in promo_candidates:
        wid = parse_id(p.get("targetId")) if p.get("kind") == "work" else None
        if wid in visible and all(w != wid for w, _ in promoted):
            promoted.append((wid, promotion_id(p)))
    promoted_ids = {w for w, _ in promoted}

    pool = FEATURED_LIMIT * 3
    sources = [
        (
            "hot_week",
            [
                (w, i + 1)
                for i, w in enumerate(
                    await _ranked_ids(session, conds, "hot_week", pool)
                )
            ],
        ),
        (
            "rising",
            [
                (w, i + 1)
                for i, w in enumerate(await _ranked_ids(session, conds, "rising", pool))
            ],
        ),
        ("new", [(w, None) for w in await _ranked_ids(session, conds, "new", pool)]),
        ("editor", [(w, None) for w in editor_ids if w in visible]),
    ]
    sources = [(r, [x for x in lst if x[0] not in promoted_ids]) for r, lst in sources]
    organic: list[tuple[int, str, int | None, str | None]] = []
    used: set[int] = set()
    taken = {r: 0 for r, _ in sources}
    cursor = {r: 0 for r, _ in sources}
    progressed = True
    while len(organic) < FEATURED_LIMIT and progressed:
        progressed = False
        for reason, lst in sources:
            if len(organic) >= FEATURED_LIMIT:
                break
            if taken[reason] >= SOURCE_TAKE[reason]:
                continue
            i = cursor[reason]
            while i < len(lst) and lst[i][0] in used:
                i += 1
            cursor[reason] = i + 1
            if i >= len(lst):
                continue
            used.add(lst[i][0])
            taken[reason] += 1
            organic.append((lst[i][0], reason, lst[i][1], None))
            progressed = True

    # 插推广：每组 4 张的第 2 个位置；推广不超过总数的 1/4
    out: list[tuple[int, str, int | None, str | None]] = []
    o = p = 0
    while len(out) < FEATURED_LIMIT and (o < len(organic) or p < len(promoted)):
        if len(out) % PROMOTION_EVERY == 1 and p < len(promoted):
            wid, pid = promoted[p]
            p += 1
            out.append((wid, "promoted", None, pid))
            continue
        if o >= len(organic):
            break
        out.append(organic[o])
        o += 1
    while sum(1 for x in out if x[1] == "promoted") > len(out) // PROMOTION_EVERY:
        last = max(i for i, x in enumerate(out) if x[1] == "promoted")
        out.pop(last)

    works = await _serialize_ids(session, ctx, [x[0] for x in out])
    return [
        {
            "id": f"{reason}:{wid}",
            "work": works[wid],
            "reason": reason,
            "rank": rank,
            "promoted": reason == "promoted",
            "promotionId": pid,
        }
        for wid, reason, rank, pid in out
        if wid in works
    ]


PROMOTABLE_EVENT_STATES = ("upcoming", "open", "judging")


async def _promoted_events(
    session, ctx, promos: list[dict], *, promotable_only: bool = False
) -> dict[str, dict]:
    """推广里的活动：targetId → 协议 Event（查看者看不到的不给；promotable_only 时只要还没结束的）。"""
    from site_v1.services import events as ev

    found = {}
    for p in promos:
        target = str(p.get("targetId") or "")
        if not target or target in found:
            continue
        e = await ev.find_event(session, target)
        if not ev.event_visible(e, ctx):
            continue
        if promotable_only and ev.event_state(e) not in PROMOTABLE_EVENT_STATES:
            continue
        found[target] = e
    if not found:
        return {}
    dtos = await ev.event_dtos(session, ctx, list(found.values()))
    return {target: dto for target, dto in zip(found, dtos)}


async def grid_promotions(
    session, ctx, board: str | None, exclude: list[str]
) -> list[dict]:
    """网格里混入的推广：作品（对查看者可见、在这个主榜里）和活动（对查看者可见、还没结束）。"""
    conds = await _type_conds(session, ctx, None, board)
    candidates = [
        p
        for p in await _active_promotions(session, "grid", exclude)
        if p.get("kind") in ("work", "event")
    ]
    ids = [parse_id(p.get("targetId")) for p in candidates if p.get("kind") == "work"]
    ids = [x for x in ids if x is not None]
    visible = (
        set(
            (
                await session.execute(select(Work.id).where(Work.id.in_(ids), *conds))
            ).scalars()
        )
        if ids
        else set()
    )
    events = await _promoted_events(
        session,
        ctx,
        [p for p in candidates if p.get("kind") == "event"],
        promotable_only=True,
    )
    chosen: list[tuple[str, str, str]] = []
    for p in candidates:
        pid = promotion_id(p)
        if any(x == pid for _, _, x in chosen):
            continue
        if p.get("kind") == "work":
            wid = parse_id(p.get("targetId"))
            if wid in visible:
                chosen.append(("work", str(wid), pid))
        elif str(p.get("targetId") or "") in events:
            chosen.append(("event", str(p.get("targetId")), pid))
        if len(chosen) >= GRID_PROMOTION_LIMIT:
            break
    works = await _serialize_ids(
        session, ctx, [int(t) for k, t, _ in chosen if k == "work"]
    )
    out = []
    for kind, target, pid in chosen:
        if kind == "work" and int(target) in works:
            out.append(
                {
                    "id": f"promo:{pid}",
                    "promotionId": pid,
                    "kind": "work",
                    "work": works[int(target)],
                }
            )
        elif kind == "event":
            out.append(
                {
                    "id": f"promo:{pid}",
                    "promotionId": pid,
                    "kind": "event",
                    "event": events[target],
                }
            )
    return out


# ---------------- 横幅 ----------------


async def active_banners(session, ctx, scope: str = "global") -> list[dict]:
    now = utc_now()
    entries: list[tuple[str, int, int | None, datetime, datetime]] = []
    if scope == "global":
        rows = (
            (
                await session.execute(
                    select(BannerCarousel)
                    .where(
                        BannerCarousel.channel_id.is_(None),
                        BannerCarousel.start_time <= now,
                        BannerCarousel.end_time > now,
                        BannerCarousel.target_type.in_((1, 3)),
                    )
                    .order_by(BannerCarousel.position, BannerCarousel.id)
                )
            )
            .scalars()
            .all()
        )
        thread_ids = [r.thread_id for r in rows if r.work_id is None and r.thread_id]
        mapping = {}
        if thread_ids:
            mapping = dict(
                (
                    await session.execute(
                        select(Thread.thread_id, Thread.work_id).where(
                            Thread.platform == "discord",
                            Thread.thread_id.in_(thread_ids),
                        )
                    )
                ).all()
            )
        app_ids = [r.application_id for r in rows if r.application_id]
        covers = {}
        if app_ids:
            covers = dict(
                (
                    await session.execute(
                        select(
                            SiteBannerMeta.application_id,
                            SiteBannerMeta.cover_attachment_id,
                        ).where(SiteBannerMeta.application_id.in_(app_ids))
                    )
                ).all()
            )
        for r in rows:
            wid = r.work_id or mapping.get(r.thread_id)
            if wid:
                entries.append(
                    (
                        f"banner_{r.id}",
                        int(wid),
                        covers.get(r.application_id),
                        r.start_time,
                        r.end_time,
                    )
                )
    else:
        rows = (
            await session.execute(
                select(BannerApplication, SiteBannerMeta)
                .join(
                    SiteBannerMeta,
                    SiteBannerMeta.application_id == BannerApplication.id,
                )
                .where(
                    BannerApplication.status == "approved",
                    BannerApplication.target_scope == scope,
                    BannerApplication.work_id.is_not(None),
                    SiteBannerMeta.start_at <= now,
                    SiteBannerMeta.end_at > now,
                )
                .order_by(SiteBannerMeta.start_at, BannerApplication.id)
            )
        ).all()
        for app, meta in rows:
            entries.append(
                (
                    f"banner_a{app.id}",
                    int(app.work_id),
                    meta.cover_attachment_id,
                    meta.start_at,
                    meta.end_at,
                )
            )
    if not entries:
        return []
    conds = await svc.listable_conditions(session, ctx)
    visible = set(
        (
            await session.execute(
                select(Work.id).where(Work.id.in_([e[1] for e in entries]), *conds)
            )
        ).scalars()
    )
    entries = [e for e in entries if e[1] in visible]
    works = await _serialize_ids(session, ctx, [e[1] for e in entries])
    extra = await load_attachments(session, [e[2] for e in entries if e[2]])
    out = []
    seen: set[str] = set()
    for bid, wid, cover_id, start, end in entries:
        if bid in seen or wid not in works:
            continue
        seen.add(bid)
        work = works[wid]
        cover = (
            attachment_dto(extra[cover_id], ctx)
            if cover_id in extra
            else work.get("cover")
        )
        out.append(
            {
                "id": bid,
                "work": work,
                "cover": cover,
                "scope": scope,
                "startAt": iso_utc(start),
                "endAt": iso_utc(end),
            }
        )
    return out


# ---------------- GET /home 等 ----------------


@router.get("/home", summary="首页：作品横幅、栏位、精选轮播、网格推广")
async def get_home(request: Request, ctx: Ctx):
    exclude = _exclude(request)
    async with get_session_factory()() as session:
        return {
            "banners": await active_banners(session, ctx),
            "rails": await home_rails(session, ctx),
            "featured": await featured_items(session, ctx, None, exclude),
            "promotions": await grid_promotions(session, ctx, None, exclude),
        }


@router.get("/home/featured", summary="精选和推荐轮播（可按资源类型）")
async def get_featured(request: Request, ctx: Ctx, type: str | None = None):
    async with get_session_factory()() as session:
        return {
            "items": await featured_items(
                session, ctx, (type or "").strip() or None, _exclude(request)
            )
        }


@router.get("/home/promotions", summary="作品网格里混入的推广")
async def get_promotions(request: Request, ctx: Ctx, board: str | None = None):
    async with get_session_factory()() as session:
        return {
            "items": await grid_promotions(
                session, ctx, (board or "").strip() or None, _exclude(request)
            )
        }


@router.get("/boards/overview", summary="tag 排行榜页：各主榜的可见作品数和周热门前 3")
async def boards_overview(ctx: Ctx):
    async with get_session_factory()() as session:
        boards = [
            b
            for b in await _boards(session)
            if b["tag"] is not None
            and (ctx.effective_mode == "r18" or not b["tag"]["restricts"])
        ]
        tag_ids = [int(b["id"]) for b in boards]
        counts = await td.visible_counts(session, ctx, tag_ids)
        tops: dict[int, list[int]] = {}
        live = [t for t in tag_ids if counts.get(t)]
        if live:
            conds = await svc.listable_conditions(session, ctx)
            rn = (
                func.row_number()
                .over(
                    partition_by=SiteWorkTag.tag_id,
                    order_by=(svc.heat_score("hot_week").desc(), Work.id.desc()),
                )
                .label("rn")
            )
            sub = (
                select(SiteWorkTag.tag_id.label("tag_id"), Work.id.label("work_id"), rn)
                .join(Work, Work.id == SiteWorkTag.work_id)
                .outerjoin(WorkStatistics, WorkStatistics.work_id == Work.id)
                .where(
                    SiteWorkTag.removed_at.is_(None),
                    SiteWorkTag.tag_id.in_(live),
                    *conds,
                )
                .subquery()
            )
            for tag_id, work_id in (
                await session.execute(
                    select(sub.c.tag_id, sub.c.work_id)
                    .where(sub.c.rn <= OVERVIEW_TOP)
                    .order_by(sub.c.tag_id, sub.c.rn)
                )
            ).all():
                tops.setdefault(int(tag_id), []).append(int(work_id))
        works = await _serialize_ids(
            session, ctx, [w for ids in tops.values() for w in ids]
        )
    items = []
    for b in boards:
        tid = int(b["id"])
        if not counts.get(tid):
            continue
        items.append(
            {
                "board": b,
                "workCount": counts[tid],
                "top": [works[w] for w in tops.get(tid, []) if w in works],
            }
        )
    return {"items": items}


@router.get("/search/suggest", summary="搜索联想：tag（带可见作品数）、作品、作者")
async def search_suggest(ctx: Ctx, q: str = ""):
    empty = {
        "tags": [],
        "works": [],
        "users": [],
        "lists": [],
        "collections": [],
        "events": [],
    }
    needle = q.strip().lower()
    if not needle:
        return empty
    async with get_session_factory()() as session:
        candidates = (
            (
                await session.execute(
                    select(SiteTag)
                    .where(
                        SiteTag.deleted_at.is_(None),
                        func.lower(SiteTag.name).contains(needle, autoescape=True),
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        counts = await td.visible_counts(session, ctx, [t.id for t in candidates])
        tags = [
            {
                "id": str(t.id),
                "name": t.name,
                "group": t.group_id,
                "count": counts[t.id],
            }
            for t in candidates
            if counts.get(t.id)
        ]
        tags.sort(key=lambda x: (x["name"].lower() != needle, -x["count"]))
        conds = await svc.listable_conditions(session, ctx)
        works = (
            await session.execute(
                select(Work.id, Work.title)
                .outerjoin(WorkStatistics, WorkStatistics.work_id == Work.id)
                .where(func.lower(Work.title).contains(needle, autoescape=True), *conds)
                .order_by(svc.heat_score("hot_week").desc(), Work.id.desc())
                .limit(5)
            )
        ).all()
        users = (
            await session.execute(
                select(User.id, User.display_name, User.username)
                .where(
                    User.merged_into_id.is_(None),
                    or_(
                        func.lower(User.display_name).contains(needle, autoescape=True),
                        func.lower(User.username).contains(needle, autoescape=True),
                    ),
                )
                .order_by(User.username)
                .limit(5)
            )
        ).all()
        # 活动：标题含关键字、查看者看得到的（R18 活动对全年龄模式不联想，草稿只给主办人和站务）
        from models.site_event import SiteEvent
        from site_v1.services import events as ev

        event_rows = (
            (
                await session.execute(
                    select(SiteEvent)
                    .where(
                        func.lower(SiteEvent.title).contains(needle, autoescape=True)
                    )
                    .order_by(SiteEvent.submit_end_at.desc(), SiteEvent.id)
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        events = [
            {"id": str(e.id), "title": e.title}
            for e in event_rows
            if ev.event_visible(e, ctx)
        ][:5]
    return {
        **empty,
        "tags": tags[:8],
        "works": [{"id": str(i), "title": t} for i, t in works],
        "users": [{"id": str(i), "displayName": d or u} for i, d, u in users],
        "events": events,
    }


@router.get(
    "/works/by-legacy/discord/{threadId}",
    summary="旧链接 /threads/{DC帖子ID} 换成新作品",
)
async def by_legacy_discord(threadId: str, ctx: Ctx):
    if not threadId.isdigit():
        raise not_found()
    async with get_session_factory()() as session:
        work_id = (
            await session.execute(
                select(Thread.work_id)
                .where(
                    Thread.platform == "discord",
                    Thread.thread_id == int(threadId),
                    Thread.work_id.is_not(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        work = await session.get(Work, work_id) if work_id else None
        if work is None:
            work = (
                (
                    await session.execute(
                        select(Work)
                        .where(
                            Work.source_platform == "discord",
                            Work.source_thread_id == threadId,
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if work is None or not await work_visible(session, ctx, work):
            raise not_found()
        return {"workId": str(work.id)}


# ---------------- 横幅接口 ----------------


@router.get("/banners", summary="当前有效的作品横幅")
async def list_banners(ctx: Ctx, scope: str = "global"):
    if scope != "global" and not scope.startswith("tag:"):
        raise invalid({"scope": "scope 只能是 global 或 tag:{tagId}"})
    async with get_session_factory()() as session:
        return {"items": await active_banners(session, ctx, scope)}


async def _application_dtos(session, ctx, rows) -> list[dict]:
    """[(BannerApplication, SiteBannerMeta|None)] → 协议 BannerApplication；作品对查看者不可见的跳过。"""
    if not rows:
        return []
    conds_ids = [a.work_id for a, _ in rows]
    works_rows = (
        (await session.execute(select(Work).where(Work.id.in_(conds_ids))))
        .scalars()
        .all()
    )
    by_id = {w.id: w for w in works_rows}
    visible = {wid for wid, w in by_id.items() if await work_visible(session, ctx, w)}
    rows = [(a, m) for a, m in rows if a.work_id in visible]
    works = await _serialize_ids(session, ctx, [a.work_id for a, _ in rows])
    users = await load_users(
        session,
        {
            x
            for a, _ in rows
            for x in (a.fluxer_owner_id or a.fluxer_applicant_id, a.fluxer_reviewer_id)
            if x
        },
    )
    covers = await load_attachments(
        session, [m.cover_attachment_id for _, m in rows if m and m.cover_attachment_id]
    )
    carousel = {}
    plain = [
        a.id
        for a, m in rows
        if a.status == "approved" and (m is None or m.start_at is None)
    ]
    if plain:
        carousel = {
            c.application_id: c
            for c in (
                await session.execute(
                    select(BannerCarousel).where(
                        BannerCarousel.application_id.in_(plain)
                    )
                )
            ).scalars()
        }
    out = []
    for a, m in rows:
        applicant = a.fluxer_owner_id or a.fluxer_applicant_id
        resolution = None
        if a.status in ("approved", "rejected"):
            start = m.start_at if m else None
            end = m.end_at if m else None
            if start is None and a.id in carousel:
                start, end = carousel[a.id].start_time, carousel[a.id].end_time
            resolution = {
                "decision": "approve" if a.status == "approved" else "reject",
                "startAt": iso_utc(start) if a.status == "approved" else None,
                "endAt": iso_utc(end) if a.status == "approved" else None,
                "reason": a.reject_reason or "",
                "by": users.get(a.fluxer_reviewer_id)
                or svc.unknown_user(a.fluxer_reviewer_id),
                "at": iso_utc(a.reviewed_at or a.applied_at),
            }
        cover_id = m.cover_attachment_id if m else None
        out.append(
            {
                "id": str(a.id),
                "work": works[a.work_id],
                "cover": attachment_dto(covers[cover_id], ctx)
                if cover_id in covers
                else None,
                "scope": a.target_scope,
                "note": (m.note if m else "") or "",
                "state": a.status
                if a.status in ("pending", "approved", "rejected")
                else "rejected",
                "applicant": users.get(applicant) or svc.unknown_user(applicant),
                "createdAt": iso_utc(a.applied_at),
                "resolution": resolution,
            }
        )
    return out


async def _valid_scope(session, scope) -> bool:
    if scope == "global":
        return True
    if not isinstance(scope, str) or not scope.startswith("tag:"):
        return False
    return (await _board_tag(session, scope[4:])) not in (False, None)


@router.post(
    "/banners/applications", status_code=201, summary="作者为自己的作品申请横幅"
)
async def apply_banner(ctx: Ctx, viewer: CurrentUser, body: dict = Body(...)):
    work_id = parse_id(body.get("workId"))
    scope = body.get("scope") or "global"
    note = body.get("note") or ""
    cover_upload = body.get("coverUploadId")
    async with get_session_factory()() as session:
        work = await session.get(Work, work_id) if work_id else None
        if (
            work is None
            or work.status != "published"
            or not await work_visible(session, ctx, work)
        ):
            raise not_found()
        if work.author_id != viewer.user_id:
            raise forbidden("只能为自己的作品申请横幅")
        if not await _valid_scope(session, scope):
            raise invalid({"scope": "横幅位置不对"})
        pending = (
            await session.execute(
                select(BannerApplication.id).where(
                    BannerApplication.work_id == work.id,
                    BannerApplication.status == "pending",
                )
            )
        ).first()
        if pending is not None:
            raise SiteError(409, "conflict", "这个作品已经有一条待审核的申请")
        cover_id = None
        if cover_upload:
            att_id = parse_id(cover_upload)
            att = await session.get(Attachment, att_id) if att_id else None
            if (
                att is None
                or att.owner_id != viewer.user_id
                or att.state != "ready"
                or att.deleted_at is not None
            ):
                raise invalid({"coverUploadId": "找不到这张图"})
            from site_v1.services.tag_rules import rating_rank

            if rating_rank(att.rating or "general") > rating_rank(work_rating(work)):
                raise invalid({"coverUploadId": "横幅图的分级高于作品分级"})
            cover_id = att.id
        app = BannerApplication(
            thread_id=None,
            channel_id=None,
            work_id=work.id,
            target_type=3,
            applicant_id=int(viewer.discord_user_id)
            if (viewer.discord_user_id or "").isdigit()
            else None,
            fluxer_applicant_id=viewer.user_id,
            fluxer_owner_id=viewer.user_id,
            target_scope=scope,
            cover_image_url=None,
            status="pending",
            applied_at=utc_now(),
        )
        session.add(app)
        await session.flush()
        session.add(
            SiteBannerMeta(
                application_id=app.id,
                cover_attachment_id=cover_id,
                note=str(note).strip()[:500],
            )
        )
        new_id = app.id
        await session.commit()
        return JSONResponse({"id": str(new_id), "state": "pending"}, status_code=201)


async def _applications(session, *where, order):
    return (
        await session.execute(
            select(BannerApplication, SiteBannerMeta)
            .outerjoin(
                SiteBannerMeta, SiteBannerMeta.application_id == BannerApplication.id
            )
            .where(BannerApplication.work_id.is_not(None), *where)
            .order_by(*order)
        )
    ).all()


@router.get("/banners/applications", summary="自己的横幅申请")
async def my_applications(
    ctx: Ctx, viewer: CurrentUser, page: PageRequest = Depends(pagination)
):
    async with get_session_factory()() as session:
        rows = await _applications(
            session,
            or_(
                BannerApplication.fluxer_owner_id == viewer.user_id,
                BannerApplication.fluxer_applicant_id == viewer.user_id,
            ),
            order=(BannerApplication.applied_at.desc(), BannerApplication.id.desc()),
        )
        items = await _application_dtos(session, ctx, rows)
    chunk = items[page.offset : page.offset + page.limit + 1]
    return page_response(chunk, page, lambda x: x)


def _require(ctx, key: str, message: str):
    viewer = ctx.require_user()
    if not viewer.has_permission(key):
        raise forbidden(message)
    return viewer


@router.get("/admin/banners/applications", summary="横幅申请审核列表（banners.review）")
async def admin_applications(
    ctx: Ctx, state: str = "pending", page: PageRequest = Depends(pagination)
):
    _require(ctx, "banners.review", "你没有审核横幅申请的权限")
    if state not in ("pending", "approved", "rejected", "resolved"):
        raise invalid({"state": "state 不对"})
    async with get_session_factory()() as session:
        if state == "pending":
            rows = await _applications(
                session,
                BannerApplication.status == "pending",
                order=(BannerApplication.applied_at, BannerApplication.id),
            )
        else:
            cond = (
                BannerApplication.status != "pending"
                if state == "resolved"
                else BannerApplication.status == state
            )
            rows = await _applications(
                session,
                cond,
                order=(
                    BannerApplication.reviewed_at.desc().nulls_last(),
                    BannerApplication.id.desc(),
                ),
            )
        # 审核员按自己的有效模式看：受限作品的申请要开 R18 模式
        items = await _application_dtos(session, ctx, rows)
    chunk = items[page.offset : page.offset + page.limit + 1]
    return page_response(chunk, page, lambda x: x)


@router.post("/admin/banners/applications/{id}/resolve", summary="通过或驳回横幅申请")
async def resolve_application(id: str, ctx: Ctx, body: dict = Body(...)):
    viewer = _require(ctx, "banners.review", "你没有审核横幅申请的权限")
    app_id = parse_id(id)
    async with get_session_factory()() as session:
        app = (
            await session.get(BannerApplication, app_id, with_for_update=True)
            if app_id
            else None
        )
        work = (
            await session.get(Work, app.work_id)
            if app is not None and app.work_id
            else None
        )
        if app is None or work is None or not await work_visible(session, ctx, work):
            raise not_found()
        if app.status != "pending":
            raise SiteError(409, "conflict", "这条申请已经处理过了，请刷新")
        decision = body.get("decision")
        reason = body.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""
        if decision not in ("approve", "reject"):
            raise invalid({"decision": "请选择通过或驳回"})
        meta = await session.get(SiteBannerMeta, app.id)
        if meta is None:
            meta = SiteBannerMeta(application_id=app.id)
            session.add(meta)
        now = utc_now()
        if decision == "approve":
            start, end = (
                _parse_time(body.get("startAt")),
                _parse_time(body.get("endAt")),
            )
            if start is None or end is None:
                raise invalid({"startAt": "请填展示的开始和结束时间"})
            if end <= start:
                raise invalid({"endAt": "结束时间要晚于开始时间"})
            if end - start > 30 * DAY:
                raise invalid({"endAt": "一次最多展示 30 天"})
            cover_id = meta.cover_attachment_id or work.cover_attachment_id
            img = await session.get(Attachment, cover_id) if cover_id else None
            if img is None or img.deleted_at is not None:
                raise invalid(
                    {"decision": "这个作品没有封面，也没有另传横幅图，不能通过"}
                )
            from site_v1.services.tag_rules import rating_rank

            if rating_rank(img.rating or "general") > rating_rank(work_rating(work)):
                raise invalid({"decision": "横幅图的分级高于作品分级，不能通过"})
            meta.start_at, meta.end_at = start, end
            if app.target_scope == "global":
                # 全站横幅同时进 V2 的轮播表（V2 首页也能看到）
                position = (
                    await session.execute(
                        select(
                            func.coalesce(func.max(BannerCarousel.position), 0)
                        ).where(BannerCarousel.channel_id.is_(None))
                    )
                ).scalar_one()
                row = BannerCarousel(
                    application_id=app.id,
                    applicant_id=app.applicant_id,
                    thread_id=None,
                    channel_id=None,
                    cover_image_url=None,
                    title=work.title,
                    target_type=3,
                    start_time=start,
                    end_time=end,
                    position=int(position) + 1,
                    fluxer_applicant_id=app.fluxer_applicant_id,
                    work_id=work.id,
                    fluxer_owner_id=app.fluxer_owner_id,
                )
                session.add(row)
                await session.flush()
                meta.carousel_id = row.id
            app.status = "approved"
            app.reject_reason = None
        else:
            if not reason:
                raise invalid({"reason": "驳回要写理由（作者会收到提醒）"})
            app.status = "rejected"
            app.reject_reason = reason[:2000]
        app.reviewed_at = now
        app.fluxer_reviewer_id = viewer.user_id
        app.reviewer_id = (
            int(viewer.discord_user_id)
            if (viewer.discord_user_id or "").isdigit()
            else None
        )
        await session.commit()
        rows = await _applications(
            session, BannerApplication.id == app_id, order=(BannerApplication.id,)
        )
        return (await _application_dtos(session, ctx, rows))[0]


# ---------------- 首页配置（home.manage） ----------------


def _require_home(ctx):
    return _require(ctx, "home.manage", "你没有配置首页的权限")


@router.get("/admin/home", summary="首页栏位配置")
async def admin_home(ctx: Ctx):
    _require_home(ctx)
    async with get_session_factory()() as session:
        return {"rails": await _rails_config(session)}


@router.put("/admin/home", summary="整组替换首页栏位")
async def put_admin_home(ctx: Ctx, body: dict = Body(...)):
    _require_home(ctx)
    rails = body.get("rails")
    if not isinstance(rails, list):
        raise invalid({"rails": "格式不对"})
    if len(rails) > MAX_RAILS:
        raise invalid({"rails": f"最多 {MAX_RAILS} 个栏位"})
    async with get_session_factory()() as session:
        clean = []
        ordered = sorted(
            (r if isinstance(r, dict) else {} for r in rails),
            key=lambda r: r.get("order")
            if isinstance(r.get("order"), (int, float))
            else 0,
        )
        for i, r in enumerate(ordered):
            at = f"rails.{i}"
            kind = r.get("kind")
            if kind not in RAIL_KINDS:
                raise invalid({at: "栏位类型不对"})
            title = r.get("title").strip() if isinstance(r.get("title"), str) else ""
            if not title or len(title) > 20:
                raise invalid({at: "标题 1 到 20 个字"})
            out = {"kind": kind, "refId": None, "title": title, "order": i}
            ref = r.get("refId")
            if kind == "board":
                if not ref or await _board_tag(session, str(ref)) is False:
                    raise invalid({at: f"「{title}」选的主榜不存在"})
                sort = r.get("sort")
                if sort and sort not in RAIL_SORTS:
                    raise invalid({at: f"「{title}」的排序不对"})
                out["refId"] = str(ref)
                out["sort"] = sort or "hot_week"
            elif kind in ("list", "event"):
                if not ref:
                    raise invalid(
                        {
                            at: f"「{title}」要填{'榜单' if kind == 'list' else '活动'} ID"
                        }
                    )
                out["refId"] = str(ref)
            elif kind == "curated":
                ids = list(
                    dict.fromkeys(str(x) for x in (r.get("workIds") or []) if str(x))
                )
                if not ids or len(ids) > 24:
                    raise invalid({at: f"「{title}」要挑 1 到 24 个作品"})
                missing = await _unpublished(session, ids)
                if missing:
                    raise invalid(
                        {
                            at: f"「{title}」里这些作品不存在或没发布：{'、'.join(missing)}"
                        }
                    )
                out["workIds"] = ids
            clean.append(out)
        await _save_setting(session, "home.rails", {"rails": clean})
        await session.commit()
    return {"rails": clean}


async def _unpublished(session, ids: list[str]) -> list[str]:
    parsed = {x: parse_id(x) for x in ids}
    ok = (
        set(
            (
                await session.execute(
                    select(Work.id).where(
                        Work.id.in_([v for v in parsed.values() if v is not None]),
                        Work.status == "published",
                        Work.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        if any(v is not None for v in parsed.values())
        else set()
    )
    return [x for x, v in parsed.items() if v not in ok]


async def _admin_featured(session, ctx) -> dict:
    store = await _featured_store(session)
    now = utc_now()
    promos = sorted(store["promotions"], key=lambda p: p.get("order", 0))
    ids = [parse_id(p.get("targetId")) for p in promos if p.get("kind") == "work"]
    ids = [x for x in ids if x is not None]
    works = {}
    if ids:
        rows = (
            (await session.execute(select(Work).where(Work.id.in_(ids))))
            .scalars()
            .all()
        )
        visible = [w for w in rows if await work_visible(session, ctx, w)]
        works = {
            int(d["id"]): d for d in await svc.serialize_works(session, ctx, visible)
        }
    event_dtos = await _promoted_events(
        session, ctx, [p for p in promos if p.get("kind") == "event"]
    )
    out = []
    for p in promos:
        item = {k: v for k, v in p.items() if k != "notified"}
        item["weight"] = p.get("weight") or DEFAULT_WEIGHT
        item["id"] = promotion_id(p)
        item["work"] = (
            works.get(parse_id(p.get("targetId"))) if p.get("kind") == "work" else None
        )
        item["event"] = (
            event_dtos.get(str(p.get("targetId"))) if p.get("kind") == "event" else None
        )
        item["state"] = promotion_state(p, now)
        item["notified"] = p.get("notified")
        out.append(item)
    return {"editorPicks": list(store["editorPicks"]), "promotions": out}


@router.get("/admin/home/featured", summary="站务挑选和推广位配置")
async def get_admin_featured(ctx: Ctx):
    _require_home(ctx)
    async with get_session_factory()() as session:
        return await _admin_featured(session, ctx)


@router.put("/admin/home/featured", summary="整组替换站务挑选和推广位")
async def put_admin_featured(ctx: Ctx, body: dict = Body(...)):
    _require_home(ctx)
    picks_raw, promos_raw = body.get("editorPicks"), body.get("promotions")
    if not isinstance(picks_raw, list) or not isinstance(promos_raw, list):
        raise invalid({"promotions": "格式不对"})
    picks = list(dict.fromkeys(str(x).strip() for x in picks_raw if str(x).strip()))
    if len(picks) > MAX_EDITOR_PICKS:
        raise invalid({"editorPicks": f"运营推荐最多 {MAX_EDITOR_PICKS} 个作品"})
    if len(promos_raw) > MAX_PROMOTIONS:
        raise invalid({"promotions": f"最多 {MAX_PROMOTIONS} 条推广"})
    async with get_session_factory()() as session:
        missing = await _unpublished(session, picks) if picks else []
        if missing:
            raise invalid(
                {
                    "editorPicks": f"运营推荐里这些作品不存在或没发布：{'、'.join(missing)}"
                }
            )
        previous = {
            promotion_id(p): p for p in (await _featured_store(session))["promotions"]
        }
        seen: set[str] = set()
        promotions = []
        for i, p in enumerate(promos_raw):
            p = p if isinstance(p, dict) else {}
            at, n = f"promotions.{i}", f"第 {i + 1} 条"
            kind, placement = p.get("kind"), p.get("placement")
            if kind not in ("work", "event"):
                raise invalid({at: f"{n}：类型只能是作品或活动"})
            if placement not in ("carousel", "grid", "both"):
                raise invalid({at: f"{n}：投放位置不对"})
            if kind == "event" and placement != "grid":
                raise invalid({at: f"{n}：活动只能投放到作品网格"})
            target = str(p.get("targetId") or "").strip()
            if kind == "work" and (not target or await _unpublished(session, [target])):
                raise invalid({at: f"{n}：作品「{target or '（空）'}」不存在或没发布"})
            if kind == "event":
                from site_v1.services import events as ev

                if not target or await ev.find_event(session, target) is None:
                    raise invalid({at: f"{n}：活动「{target or '（空）'}」不存在"})
            key = f"{kind}:{target}"
            if key in seen:
                raise invalid(
                    {at: f"{n}：作品 {target} 重复了，同一对象只能有一条推广"}
                )
            seen.add(key)
            weight = p.get("weight", DEFAULT_WEIGHT)
            if weight is None:
                weight = DEFAULT_WEIGHT
            if (
                isinstance(weight, bool)
                or not isinstance(weight, int)
                or not 1 <= weight <= 10
            ):
                raise invalid({at: f"{n}：权重是 1 到 10 的整数"})
            start, end = _parse_time(p.get("startAt")), _parse_time(p.get("endAt"))
            if start is None or end is None:
                raise invalid({at: f"{n}：请填开始和结束时间"})
            if end <= start:
                raise invalid({at: f"{n}：结束时间要晚于开始时间"})
            if end - start > 30 * DAY:
                raise invalid({at: f"{n}：一次最多推广 30 天"})
            notify_text = str(p.get("notifyText") or "").strip()
            if len(notify_text) > 60:
                raise invalid({at: f"{n}：推送文案最多 60 个字"})
            promotions.append(
                {
                    "kind": kind,
                    "targetId": target,
                    "placement": placement,
                    "weight": weight,
                    "startAt": iso_utc(start),
                    "endAt": iso_utc(end),
                    "order": i,
                    "note": str(p.get("note") or "").strip()[:100],
                    "notify": bool(p.get("notify")),
                    "notifyText": notify_text,
                    # 推送到提醒（「运营推荐」提醒）要等提醒模块接上，先只保留旧的推送记录
                    "notified": (previous.get(key) or {}).get("notified"),
                }
            )
        await _save_setting(
            session, "home.featured", {"editorPicks": picks, "promotions": promotions}
        )
        await session.commit()
        return await _admin_featured(session, ctx)
