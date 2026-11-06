"""作品领域（协议 3.2）：可见性、列表查询、Work 序列化、tag 增删、分级同步、发布。

数据就是 V2 的 work 表（加了 site_v1_schema 的协议字段），统计复用 work_statistics，
收藏复用 V2 的默认书单（WorkService.collect），关注复用 thread_follow。

可见性（3.12）：详情判断和管理权限在 services/work_access.py（全 /api/v1 唯一一份），
这里是列表的 SQL 条件，语义一致：
- 列表只给 status=published、没删除、moderation_state 不是 hidden/removed、来源可见的作品。
  「来源可见」= source_visible，或者是还没建好原生主帖的 Fluxer 作品（可见性只看本地状态，
  原生帖没建好只影响聊天）；
- 分级由 is_nsfw/is_abyss 得出，restricted = 分级不是 general 或绑了 restricts=true 的 tag；
- 有效模式 general 时 restricted 一律不给；r18g 只在 showR18G 时给；rating 参数只能收窄；
- 过滤屏蔽的 tag（prefs.blockedTags）和屏蔽的用户；
- 详情：草稿、隐藏的只有作者/有 works.manage_any 的人能看；看不到的一律 404。

/api/v1 建的草稿：source_platform=fluxer、source_visible=false、status=draft。V2 列表因为
source_visible=false 天然看不到草稿。发布时真实环境排 V2 的 create_main 任务（建原生主帖，
主帖投影触发器会把 source_visible 置真）；FLUXER_FAKE=1 时直接把 source_visible 置真（视为同步成功）。

改分级（tag 换了分级）：有 Fluxer 帖子且不是 fake 模式时走 WorkService.update（排 move 任务，
帖子在 4 个论坛间移动）；否则直接改 is_nsfw/is_abyss。
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, false, func, literal, or_, select, tuple_

from models.booklist import Booklist
from models.booklist_item import BooklistItem
from models.site_tag import SiteTag, SiteWorkTag
from models.thread import Thread
from models.thread_follow import ThreadFollow
from models.work import Work
from models.work_statistics import WorkStatistics
from models.work_task import WorkTask
from shared.time_utils import utc_now
from site_v1 import mode as modes
from site_v1.config import get_config
from site_v1.dto import decorations_dto, iso_utc, load_users
from site_v1.errors import SiteError, forbidden, invalid
from site_v1.paging import PageRequest, encode_cursor
from site_v1.services import social
from site_v1.services import tag_rules as rules
from site_v1.services.attachments import attachment_dto, load_attachments
from site_v1.services.work_access import (  # noqa: F401  统一的可见性和权限，这里转出旧名字
    HIDDEN_MODERATION,
    LIVE_STATUSES,
    get_editable_work,
    get_visible_work,
    parse_id,
    restricted_ids,
    source_ok,
    visible_to,
)
from site_v1.services.work_access import can_manage_work as can_manage
from site_v1.services.work_access import work_rating as rating_of
from site_v1.services.versions import (
    latest_version_row,
    latest_versions,
    load_version_files,
)

logger = logging.getLogger(__name__)

WORK_SORTS = ("hot_day", "hot_week", "hot_month", "new", "rising", "hall", "latest")
SEARCH_SORTS = (*WORK_SORTS, "relevance", "favorites")
# 热度的半衰期（天）：hot_day / hot_week / hot_month / rising
HALF_LIFE_DAYS = {"hot_day": 1, "hot_week": 7, "hot_month": 30, "rising": 2}
HALL_MIN_DAYS = 90


# ---------------- 基础判断 ----------------


def parse_csv(raw) -> list[str]:
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for item in items:
        out.extend(x.strip() for x in str(item).split(",") if x.strip())
    return out


def restricting_tag_exists():
    """作品绑了 restricts=true 的 tag（SQL 条件）。"""
    return (
        select(literal(1))
        .select_from(SiteWorkTag)
        .join(SiteTag, SiteTag.id == SiteWorkTag.tag_id)
        .where(
            SiteWorkTag.work_id == Work.id,
            SiteWorkTag.removed_at.is_(None),
            SiteTag.restricts.is_(True),
            SiteTag.deleted_at.is_(None),
        )
        .exists()
    )


def has_tag(tag_id: int):
    return exists().where(
        SiteWorkTag.work_id == Work.id,
        SiteWorkTag.removed_at.is_(None),
        SiteWorkTag.tag_id == tag_id,
    )


def has_any_tag(tag_ids: list[int]):
    return exists().where(
        SiteWorkTag.work_id == Work.id,
        SiteWorkTag.removed_at.is_(None),
        SiteWorkTag.tag_id.in_(tag_ids),
    )


def rating_condition(ratings: set[str]):
    parts = []
    if modes.GENERAL in ratings:
        parts.append(and_(Work.is_nsfw.is_(False), Work.is_abyss.is_(False)))
    if modes.R18 in ratings:
        parts.append(and_(Work.is_nsfw.is_(True), Work.is_abyss.is_(False)))
    if modes.R18G in ratings:
        parts.append(Work.is_abyss.is_(True))
    return or_(*parts) if parts else false()


# ---------------- 列表 ----------------


async def listable_conditions(
    session,
    ctx,
    *,
    ratings=None,
    tags=None,
    tag_logic="and",
    exclude_tags=None,
    type_name=None,
) -> list:
    """3.12 的列表过滤条件（只给已发布、当前查看者能看到的作品）。"""
    conds = [
        Work.deleted_at.is_(None),
        Work.status == "published",
        Work.moderation_state.not_in(HIDDEN_MODERATION),
        or_(
            Work.source_visible.is_(True),
            and_(Work.source_platform == "fluxer", Work.source_thread_id == ""),
        ),
    ]
    allowed = modes.narrow_ratings(ratings, ctx.effective_mode, ctx.show_r18g)
    conds.append(rating_condition(allowed))
    if ctx.effective_mode != modes.R18:
        conds.append(~restricting_tag_exists())
    tag_ids = [parse_id(t) for t in (tags or [])]
    if tag_ids:
        if any(t is None for t in tag_ids) and tag_logic != "or":
            conds.append(false())
        valid = [t for t in tag_ids if t is not None]
        if tag_logic == "or":
            conds.append(has_any_tag(valid) if valid else false())
        else:
            conds.extend(has_tag(t) for t in valid)
    excluded = [parse_id(t) for t in (exclude_tags or [])]
    viewer = ctx.viewer
    if viewer is not None:
        excluded.extend(parse_id(t) for t in viewer.prefs.blocked_tags)
    excluded = [t for t in excluded if t is not None]
    if excluded:
        conds.append(~has_any_tag(excluded))
    if type_name:
        type_id = (
            await session.execute(
                select(SiteTag.id).where(
                    SiteTag.group_id == "type",
                    SiteTag.name == type_name,
                    SiteTag.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        conds.append(has_tag(type_id) if type_id is not None else false())
    if viewer is not None:
        blocked = await social.blocked_user_ids(session, viewer.user_id)
        if blocked:
            conds.append(
                or_(Work.author_id.is_(None), Work.author_id.not_in(list(blocked)))
            )
    return conds


def _utc_now_sql():
    return func.timezone("utc", func.now())


def heat_score(sort: str):
    """热度分（只用来排序，不下发）：互动加权 × 按最近活跃时间的半衰期衰减。"""
    raw = (
        func.coalesce(WorkStatistics.reaction_count, 0)
        + 2 * func.coalesce(WorkStatistics.collection_count, 0)
        + 3 * func.coalesce(WorkStatistics.download_count, 0)
        + 0.2 * func.coalesce(WorkStatistics.reply_count, 0)
    )
    anchor = func.coalesce(Work.last_active_at, Work.published_at, Work.created_at)
    age_days = func.extract("epoch", _utc_now_sql() - anchor) / 86400.0
    half_life = HALF_LIFE_DAYS.get(sort, 7)
    return raw * func.power(0.5, func.greatest(age_days, 0) / half_life)


def _parse_dt(raw) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise invalid({"cursor": "游标无效，请从第一页重新开始"})
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _keyset(page: PageRequest):
    after = page.after
    if after is None:
        return None
    if len(after) != 2 or not isinstance(after[1], int):
        raise invalid({"cursor": "游标无效，请从第一页重新开始"})
    return _parse_dt(after[0]), after[1]


async def query_page(
    session, ctx, conds: list, page: PageRequest, sort: str, *, relevance=None
) -> dict:
    """按 sort 排序取一页并序列化成 WorkPage。"""
    q = (
        select(Work)
        .outerjoin(WorkStatistics, WorkStatistics.work_id == Work.id)
        .where(*conds)
    )
    key_col = None
    if sort == "latest":
        key_col = Work.updated_at
    elif sort == "new":
        key_col = func.coalesce(Work.published_at, Work.created_at)
    if key_col is not None:
        keyset = _keyset(page)
        if keyset is not None:
            q = q.where(tuple_(key_col, Work.id) < tuple_(keyset[0], keyset[1]))
        q = q.order_by(key_col.desc(), Work.id.desc()).limit(page.limit + 1)
        rows = (await session.execute(q.add_columns(key_col))).all()
        works = [r[0] for r in rows]
        has_more = len(works) > page.limit
        works = works[: page.limit]
        items = await serialize_works(session, ctx, works)
        next_cursor = None
        if has_more and works:
            last_key = rows[len(works) - 1][1]
            next_cursor = encode_cursor({"k": [last_key.isoformat(), works[-1].id]})
        return {"items": items, "nextCursor": next_cursor}

    if sort in ("favorites", "hall"):
        if sort == "hall":
            q = q.where(
                func.coalesce(Work.published_at, Work.created_at)
                <= utc_now() - timedelta(days=HALL_MIN_DAYS)
            )
        order = [func.coalesce(WorkStatistics.collection_count, 0).desc()]
    elif sort == "relevance" and relevance is not None:
        order = [relevance.desc(), heat_score("hot_week").desc()]
    else:
        order = [heat_score(sort).desc()]
    q = q.order_by(*order, Work.id.desc()).offset(page.offset).limit(page.limit + 1)
    works = list((await session.execute(q)).scalars().all())
    has_more = len(works) > page.limit
    works = works[: page.limit]
    items = await serialize_works(session, ctx, works)
    next_cursor = (
        encode_cursor({"o": page.offset + len(works)}) if has_more and works else None
    )
    return {"items": items, "nextCursor": next_cursor}


# ---------------- 热度标记 ----------------

_hot_cache: dict = {"at": 0.0, "ids": frozenset()}
HOT_CACHE_SECONDS = 300


async def hot_ids(session) -> frozenset:
    """「热门」标记：已发布作品里周热度前 15%（分数大于 0），5 分钟缓存；不随查看者变化。"""
    now = time.monotonic()
    if now - _hot_cache["at"] < HOT_CACHE_SECONDS:
        return _hot_cache["ids"]
    total = (
        await session.execute(
            select(func.count())
            .select_from(Work)
            .where(Work.status == "published", Work.deleted_at.is_(None))
        )
    ).scalar_one()
    quota = min(200, max(1, round(total * 0.15))) if total else 0
    ids: frozenset = frozenset()
    if quota:
        score = heat_score("hot_week")
        rows = await session.execute(
            select(Work.id)
            .outerjoin(WorkStatistics, WorkStatistics.work_id == Work.id)
            .where(Work.status == "published", Work.deleted_at.is_(None), score > 0)
            .order_by(score.desc())
            .limit(quota)
        )
        ids = frozenset(rows.scalars())
    _hot_cache.update(at=now, ids=ids)
    return ids


def reset_hot_cache() -> None:
    _hot_cache.update(at=0.0, ids=frozenset())


# ---------------- 序列化 ----------------


def unknown_user(user_id: str | None) -> dict:
    return {
        "id": str(user_id or "0"),
        "username": "unknown",
        "displayName": "未知作者",
        "avatar": None,
        "decorations": decorations_dto(),
        "createdAt": "1970-01-01T00:00:00.000Z",
        "discordLinked": False,
        "isNewcomer": False,
    }


def tag_ref(info: rules.TagInfo, added_by: str | None) -> dict:
    return {
        "id": info.id,
        "name": info.name,
        "restricts": info.restricts,
        "group": info.group,
        "addedBy": added_by,
        "official": info.official,
    }


async def work_tags(session, work_ids) -> dict[int, list[dict]]:
    """{work_id: [TagRef]}，按加上的顺序。"""
    ids = list({int(i) for i in work_ids})
    out: dict[int, list[dict]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = await session.execute(
        select(SiteWorkTag.work_id, SiteWorkTag.added_by, SiteTag)
        .join(SiteTag, SiteTag.id == SiteWorkTag.tag_id)
        .where(SiteWorkTag.work_id.in_(ids), SiteWorkTag.removed_at.is_(None))
        .order_by(SiteWorkTag.id)
    )
    for work_id, added_by, tag in rows:
        out[work_id].append(tag_ref(rules.tag_info_of(tag), added_by))
    return out


async def serialize_works(session, ctx, works: list[Work]) -> list[dict]:
    """批量序列化成协议 Work（每类关联一次查询）。"""
    if not works:
        return []
    ids = [w.id for w in works]
    viewer = ctx.viewer
    authors = await load_users(session, [w.author_id for w in works if w.author_id])
    tags = await work_tags(session, ids)
    stats = {
        s.work_id: s
        for s in (
            await session.execute(
                select(WorkStatistics).where(WorkStatistics.work_id.in_(ids))
            )
        ).scalars()
    }
    media_ids = []
    for w in works:
        if w.cover_attachment_id:
            media_ids.append(w.cover_attachment_id)
        media_ids.extend(w.gallery_attachment_ids or [])
    media = await load_attachments(session, media_ids)
    versions = await latest_versions(session, ids, ctx)
    favorited: set[int] = set()
    followed: set[int] = set()
    if viewer is not None:
        favorited = set(
            (
                await session.execute(
                    select(BooklistItem.work_id)
                    .join(Booklist, Booklist.id == BooklistItem.booklist_id)
                    .where(
                        Booklist.fluxer_owner_id == viewer.user_id,
                        Booklist.is_default.is_(True),
                        BooklistItem.work_id.in_(ids),
                    )
                )
            ).scalars()
        )
        followed = set(
            (
                await session.execute(
                    select(ThreadFollow.work_id).where(
                        ThreadFollow.fluxer_user_id == viewer.user_id,
                        ThreadFollow.work_id.in_(ids),
                        ThreadFollow.active_flag.is_(True),
                    )
                )
            ).scalars()
        )
    hot = await hot_ids(session)
    result = []
    for w in works:
        rating = rating_of(w)
        refs = tags.get(w.id, [])
        visibility = modes.work_visibility(rating, any(t["restricts"] for t in refs))
        stat = stats.get(w.id)
        cover = media.get(w.cover_attachment_id) if w.cover_attachment_id else None
        latest = versions.get(w.id)
        manage = can_manage(w, viewer)
        role = "author" if viewer and w.author_id == viewer.user_id else "none"
        lock = w.rating_lock if isinstance(w.rating_lock, dict) else None
        result.append(
            {
                "id": str(w.id),
                "title": w.title,
                "summary": w.summary or "",
                "description": w.description or "",
                "author": authors.get(w.author_id) or unknown_user(w.author_id),
                "status": w.status,
                "rating": rating,
                "visibility": visibility,
                "declarations": list(w.declarations or []),
                "ratingLock": {
                    "by": str(lock.get("by") or ""),
                    "at": lock.get("at"),
                    "reason": lock.get("reason") or "",
                }
                if lock
                else None,
                "moderationState": w.moderation_state or "ok",
                "tags": refs,
                "tagLocked": bool(w.tag_locked),
                "cover": attachment_dto(cover, ctx) if cover else None,
                "gallery": [
                    attachment_dto(media[i], ctx)
                    for i in (w.gallery_attachment_ids or [])
                    if i in media
                ],
                "latestVersion": latest,
                "stats": {
                    "favorites": int(getattr(stat, "collection_count", 0) or 0),
                    "downloads": int(getattr(stat, "download_count", 0) or 0),
                    "chatMessages": int(getattr(stat, "reply_count", 0) or 0),
                },
                "layout": w.layout,
                "badges": ["hot"] if w.id in hot else [],
                "createdAt": iso_utc(w.created_at),
                "updatedAt": iso_utc(w.updated_at or w.created_at),
                "viewer": {
                    "favorited": w.id in favorited,
                    "followed": w.id in followed,
                    "role": role,
                    "canEdit": manage,
                    "canModerate": manage,
                    # 公告、规则、版本确认是第二期（/works/{id}/ack），先恒为 false
                    "needsAck": {
                        "announcement": False,
                        "rules": False,
                        "version": False,
                    },
                },
            }
        )
    return result


async def serialize_work(session, ctx, work: Work) -> dict:
    return (await serialize_works(session, ctx, [work]))[0]


# ---------------- tag 变更 ----------------


async def active_tag_rows(session, work_id: int) -> list[SiteWorkTag]:
    return list(
        (
            await session.execute(
                select(SiteWorkTag)
                .where(SiteWorkTag.work_id == work_id, SiteWorkTag.removed_at.is_(None))
                .order_by(SiteWorkTag.id)
            )
        )
        .scalars()
        .all()
    )


async def remember_tags(session, catalog: rules.Catalog, tag_ids) -> None:
    """把目录外的 tag（自由 tag）补进 catalog.by_id，规则判断要用。"""
    missing = [parse_id(i) for i in tag_ids if catalog.info(str(i)) is None]
    missing = [i for i in missing if i is not None]
    if not missing:
        return
    for row in (
        await session.execute(select(SiteTag).where(SiteTag.id.in_(missing)))
    ).scalars():
        catalog.remember(rules.tag_info_of(row))


async def has_fluxer_threads(session, work_id: int) -> bool:
    return (
        await session.execute(
            select(Thread.id)
            .where(
                Thread.work_id == work_id,
                Thread.platform == "fluxer",
                Thread.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None


def _work_forums() -> dict:
    try:
        from api.v2.config import get_work_config

        return get_work_config().get("work_forums", {}) or {}
    except Exception:  # 没有 config.json（本地 fake 环境）
        return {}


async def change_classification(session, ctx, work: Work, rating: str) -> None:
    """改分级的统一入口（作者改 tag、站务审核改分级都走这里），见 set_classification。"""
    await set_classification(
        session,
        work,
        rating,
        actor=ctx.viewer,
        mode=ctx.effective_mode,
        is_admin=ctx.viewer is not None and ctx.viewer.has_permission("works.manage_any"),
    )


async def set_classification(
    session, work: Work, rating: str, *, actor, mode: str, is_admin: bool
) -> None:
    """改分级 = 改 is_nsfw/is_abyss。有 Fluxer 帖子且不是 fake 模式时走 V2 的
    WorkService.update（排分类移动任务，帖子在 4 个论坛之间移动）；否则直接改字段。

    注意 WorkService.update 会 refresh(work)（先 flush 已有改动，不会丢），
    调用方最好在改 work 其他字段之前调这里。分级 tag、visibility 由调用方同步。
    """
    is_nsfw, is_abyss = modes.rating_flags(rating)
    if (bool(work.is_nsfw), bool(work.is_abyss)) == (is_nsfw, is_abyss):
        return
    if (
        actor is not None
        and not get_config().fluxer_fake
        and await has_fluxer_threads(session, work.id)
    ):
        from work.work_service import WorkService

        principal = actor.to_v2_principal(mode)
        principal["is_admin"] = is_admin
        await WorkService(session).update(
            work, {"is_nsfw": is_nsfw, "is_abyss": is_abyss}, principal, _work_forums()
        )
        return
    # 没有原生帖子（草稿、DC 作品）或 fake 模式：直接改，移动视为同步完成
    work.is_nsfw = is_nsfw
    work.is_abyss = is_abyss
    work.classification_revision = (work.classification_revision or 0) + 1


async def apply_tag_ids(
    session,
    ctx,
    work: Work,
    catalog: rules.Catalog,
    old_ids: list[str],
    new_ids: list[str],
) -> None:
    """把作品上的 tag 改成 new_ids（移除的保留记录），并同步分级和 visibility。"""
    new_rating = rules.rating_from_ids(catalog, new_ids)
    old_rating = rules.rating_from_ids(catalog, old_ids)
    if new_rating is not None:
        rating = new_rating
    elif old_rating is not None:
        rating = modes.GENERAL  # 分级 tag 删掉了：按全年龄
    else:
        rating = rating_of(work)  # 一直没有分级 tag（V2/DC 作品）：保持原分级
    await change_classification(session, ctx, work, rating)

    actor = ctx.viewer.user_id if ctx.viewer else None
    now = utc_now()
    rows = await active_tag_rows(session, work.id)
    current = {str(r.tag_id): r for r in rows}
    for tag_id, row in current.items():
        if tag_id not in new_ids:
            row.removed_at = now
            row.removed_by = actor
    await session.flush()
    for tag_id in new_ids:
        if tag_id not in current:
            session.add(
                SiteWorkTag(
                    work_id=work.id, tag_id=int(tag_id), added_by=actor, created_at=now
                )
            )
    work.visibility = modes.work_visibility(
        rating, rules.has_restricting(catalog, new_ids)
    )
    work.updated_at = now
    await session.flush()


async def resolve_tag_request(session, catalog: rules.Catalog, body: dict, viewer):
    """{tagId} 或 {name} → tag ID（字符串）。name 对上官方 tag（不分大小写）用官方的，否则当自由 tag。"""
    tag_id = body.get("tagId")
    name = body.get("name")
    if tag_id is not None and tag_id != "":
        tid = str(tag_id)
        if catalog.info(tid) is None:
            await remember_tags(session, catalog, [tid])
        info = catalog.info(tid)
        if info is None:
            raise invalid({"tagId": "没有这个 tag"})
        return tid
    raw = name.strip() if isinstance(name, str) else ""
    if not raw:
        raise invalid({"name": "请给出 tagId 或 name"})
    official = catalog.official_by_name(raw)
    if official is not None:
        return official.id
    # 同义词归到主 tag；被禁用的名字不能再建（tag 治理，services/tag_directory）
    from site_v1.services import tag_directory

    alias = await tag_directory.alias_target(session, raw)
    if alias is not None:
        info = rules.tag_info_of(alias)
        catalog.remember(info)
        return info.id
    if await tag_directory.disabled_name(session, raw) is not None:
        raise invalid({"name": f"「{raw}」已被禁用"})
    error = rules.validate_free_tag_name(raw)
    if error:
        raise invalid({"name": error}, error)
    row = (
        (
            await session.execute(
                select(SiteTag).where(
                    SiteTag.group_id == rules.FREE,
                    SiteTag.name == raw,
                    SiteTag.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        from sqlalchemy.dialects.postgresql import insert

        await session.execute(
            insert(SiteTag)
            .values(
                group_id=rules.FREE,
                name=raw,
                restricts=False,
                official=False,
                description="",
                sort_order=0,
                created_by=viewer.user_id,
                created_at=utc_now(),
            )
            .on_conflict_do_nothing()
        )
        row = (
            (
                await session.execute(
                    select(SiteTag).where(
                        SiteTag.group_id == rules.FREE,
                        SiteTag.name == raw,
                        SiteTag.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
    info = rules.tag_info_of(row)
    catalog.remember(info)
    return info.id


async def current_tag_ids(session, catalog: rules.Catalog, work_id: int) -> list[str]:
    ids = [str(r.tag_id) for r in await active_tag_rows(session, work_id)]
    await remember_tags(session, catalog, ids)
    return ids


async def added_by_of(session, work_id: int, tag_id: int) -> str | None:
    return (
        await session.execute(
            select(SiteWorkTag.added_by).where(
                SiteWorkTag.work_id == work_id,
                SiteWorkTag.tag_id == tag_id,
                SiteWorkTag.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def is_downgrade(catalog, before, after) -> bool:
    return rules.rating_rank(
        rules.rating_from_ids(catalog, after) or "general"
    ) < rules.rating_rank(rules.rating_from_ids(catalog, before) or "general")


def review_required(message: str) -> SiteError:
    return forbidden(message, reason="review_required")


# ---------------- 附件和分级冲突 ----------------


def is_image(att) -> bool:
    return att.kind == "image" or (att.mime or "").startswith("image/")


async def work_attachments(session, work: Work) -> list:
    """作品用到的全部附件：封面、图库、最新版本文件。"""
    ids = []
    if work.cover_attachment_id:
        ids.append(work.cover_attachment_id)
    ids.extend(work.gallery_attachment_ids or [])
    found = await load_attachments(session, ids)
    out = [found[i] for i in ids if i in found]
    latest = await latest_version_row(session, work.id)
    if latest is not None:
        out.extend((await load_version_files(session, [latest.id])).get(latest.id, []))
    return out


def rating_conflicts(work_rating: str, attachments) -> list[dict]:
    return [
        {"attachmentId": str(a.id), "name": a.filename, "rating": a.rating}
        for a in attachments
        if is_image(a)
        and a.state != "rejected"
        and rules.rating_rank(a.rating) > rules.rating_rank(work_rating)
    ]


def rating_conflict_error(work_rating: str, conflicts: list[dict]) -> SiteError:
    image_rating = modes.max_rating(c["rating"] for c in conflicts)
    return SiteError(
        422,
        "rating_conflict",
        f"有 {len(conflicts)} 张图的分级（{rules.RATING_LABEL[image_rating]}）高于作品分级"
        f"（{rules.RATING_LABEL[work_rating]}），请换图或改分级",
        {
            "workRating": work_rating,
            "imageRating": image_rating,
            "conflicts": conflicts,
            "options": ["replace_image", "raise_rating"],
            "suggestedRating": image_rating,
        },
    )


# ---------------- 发布 ----------------


def enqueue_create_main(session, work: Work, actor_id: str) -> None:
    """真实环境：排 V2 的 create_main 任务建原生主帖（和 V2 POST /v2/works 同一个任务）。"""
    forums = _work_forums()
    key = ("abyss" if work.is_abyss else "normal") + ("_nsfw" if work.is_nsfw else "")
    channel, guild = forums.get(key), forums.get("guild_id")
    if not channel or not guild:
        logger.warning(
            "作品 %s 发布了，但 Fluxer 作品论坛未配置，没有排建帖任务", work.id
        )
        return
    work.source_guild_id = str(guild)
    work.source_channel_id = str(channel)
    session.add(
        WorkTask(
            work_id=work.id,
            kind="create_main",
            dedupe_key="main:" + str(work.id),
            payload={
                "actor_id": actor_id,
                "title": work.title,
                "body": work.description or work.summary or work.title,
                "attachments": [],
                "guild_id": str(guild),
                "channel_id": str(channel),
            },
        )
    )

