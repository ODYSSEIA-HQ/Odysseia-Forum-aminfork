"""提醒（协议 3.7）：两处来源合在一起按时间倒序分页。

- V2 的 notification（和 V1/V2 共用已读事实）：作品公告、旧作品更新 → work_update；
  关注的作者发新作 → followed_author_new_work。标签审核（tag_review）是 V2 治理用的，这里不出。
- site_notification：/api/v1 新产生的提醒（好友申请、规则更新……），分类直接是协议的 category。

R18：来源作品不在当前查看者的列表范围里（受限作品在全年龄模式、R18G 没开、屏蔽的 tag/作者）的提醒
不返回、不计数、不在「全部已读」里改；切到 R18 模式后照常出现。ID：site 的是 `n<id>`，V2 的是 `v<id>`。
"""

from datetime import datetime

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    func,
    literal,
    null,
    or_,
    select,
    tuple_,
    union_all,
    update,
)

from models.notification import Notification
from models.site_me import SiteNotification
from models.thread import Thread
from models.thread_update import ThreadUpdate
from models.work import Work
from models.work_announcement import WorkAnnouncement
from shared.time_utils import utc_now
from site_v1.dto import iso_utc, load_users
from site_v1.errors import invalid
from site_v1.paging import PageRequest, encode_cursor
from site_v1.services import social
from site_v1.services.works import listable_conditions

CATEGORIES = (
    "mention",
    "reply",
    "work_update",
    "own_work_new_post",
    "followed_reply",
    "interaction",
    "system",
    "label_request",
    "moderation",
    "event",
    "followed_author_new_work",
    "followed_tag_new_work",
    "ops_recommend",
)

V2_TYPES = ("work_announcement", "thread_update", "author_new_thread")


def parse_categories(raw) -> list[str] | None:
    """逗号分隔的分类（提醒页「系统通知」一栏合并几个分类）；有不认识的返回 None。"""
    if raw is None or raw == "":
        return []
    if not isinstance(raw, str):
        return None
    parts = [p for p in raw.split(",") if p]
    if not parts or any(p not in CATEGORIES for p in parts):
        return None
    return parts


def _recipient(viewer):
    conds = [Notification.fluxer_user_id == viewer.user_id]
    discord = viewer.discord_user_id
    if discord and str(discord).isdigit():
        conds.append(
            and_(
                Notification.fluxer_user_id.is_(None),
                Notification.user_id == int(discord),
            )
        )
    return or_(*conds)


def _v2_work_id():
    by_thread = (
        select(Thread.work_id)
        .where(Thread.thread_id == Notification.thread_id)
        .order_by(Thread.id)
        .limit(1)
        .correlate(Notification)
        .scalar_subquery()
    )
    by_announcement = (
        select(WorkAnnouncement.work_id)
        .where(
            Notification.event_type == "work_announcement",
            WorkAnnouncement.id == Notification.event_source_id,
        )
        .correlate(Notification)
        .scalar_subquery()
    )
    return func.coalesce(Notification.work_id, by_announcement, by_thread)


async def union_query(session, ctx):
    """当前查看者能看到的全部提醒（src, id, created_at, read_at, category, work_id）。"""
    viewer = ctx.viewer
    site_q = select(
        literal("n").label("src"),
        SiteNotification.id.label("id"),
        SiteNotification.created_at.label("created_at"),
        SiteNotification.read_at.label("read_at"),
        cast(SiteNotification.category, String).label("category"),
        SiteNotification.work_id.label("work_id"),
        SiteNotification.actor_id.label("actor_id"),
    ).where(SiteNotification.user_id == viewer.user_id)
    v2_q = select(
        literal("v").label("src"),
        Notification.id.label("id"),
        Notification.created_at.label("created_at"),
        Notification.read_at.label("read_at"),
        case(
            (
                Notification.event_type == "author_new_thread",
                "followed_author_new_work",
            ),
            else_="work_update",
        ).label("category"),
        _v2_work_id().label("work_id"),
        cast(null(), String).label("actor_id"),
    ).where(_recipient(viewer), Notification.event_type.in_(V2_TYPES))
    u = union_all(site_q, v2_q).subquery()
    listable = select(Work.id).where(*(await listable_conditions(session, ctx)))
    conds = [
        or_(u.c.work_id.is_(None), u.c.work_id.in_(listable)),
    ]
    # V2 的作品提醒必须定位到作品（定位不到的旧数据不出）
    conds.append(or_(u.c.src == "n", u.c.work_id.is_not(None)))
    blocked = await social.blocked_user_ids(session, viewer.user_id)
    if blocked:
        conds.append(or_(u.c.actor_id.is_(None), u.c.actor_id.not_in(list(blocked))))
    return u, conds


def _parse_cursor(page: PageRequest):
    after = page.after
    if after is None:
        return None
    try:
        at, src, rid = after
        value = datetime.fromisoformat(at)
        if src not in ("n", "v") or not isinstance(rid, int):
            raise ValueError
    except (TypeError, ValueError):
        raise invalid({"cursor": "游标无效，请从第一页重新开始"})
    return value, src, rid


async def list_notifications(
    session, ctx, page: PageRequest, categories: list[str], unread_only: bool
) -> dict:
    u, conds = await union_query(session, ctx)
    if categories:
        conds.append(u.c.category.in_(categories))
    if unread_only:
        conds.append(u.c.read_at.is_(None))
    cursor = _parse_cursor(page)
    if cursor is not None:
        conds.append(tuple_(u.c.created_at, u.c.src, u.c.id) < tuple_(*cursor))
    rows = (
        await session.execute(
            select(u)
            .where(*conds)
            .order_by(u.c.created_at.desc(), u.c.src.desc(), u.c.id.desc())
            .limit(page.limit + 1)
        )
    ).all()
    has_more = len(rows) > page.limit
    rows = rows[: page.limit]
    items = await _hydrate(session, rows)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            {"k": [last.created_at.isoformat(), last.src, int(last.id)]}
        )
    return {"items": items, "nextCursor": next_cursor}


async def _hydrate(session, rows) -> list[dict]:
    site_ids = [r.id for r in rows if r.src == "n"]
    v2_ids = [r.id for r in rows if r.src == "v"]
    site = (
        {
            n.id: n
            for n in (
                await session.execute(
                    select(SiteNotification).where(SiteNotification.id.in_(site_ids))
                )
            ).scalars()
        }
        if site_ids
        else {}
    )
    v2 = (
        {
            n.id: n
            for n in (
                await session.execute(
                    select(Notification).where(Notification.id.in_(v2_ids))
                )
            ).scalars()
        }
        if v2_ids
        else {}
    )
    ann_ids = [
        n.event_source_id for n in v2.values() if n.event_type == "work_announcement"
    ]
    upd_ids = [
        n.event_source_id for n in v2.values() if n.event_type == "thread_update"
    ]
    anns = (
        {
            a.id: a
            for a in (
                await session.execute(
                    select(WorkAnnouncement).where(WorkAnnouncement.id.in_(ann_ids))
                )
            ).scalars()
        }
        if ann_ids
        else {}
    )
    upds = (
        {
            a.id: a
            for a in (
                await session.execute(
                    select(ThreadUpdate).where(ThreadUpdate.id.in_(upd_ids))
                )
            ).scalars()
        }
        if upd_ids
        else {}
    )
    work_ids = {r.work_id for r in rows if r.work_id}
    works = (
        {
            w.id: w
            for w in (
                await session.execute(select(Work).where(Work.id.in_(work_ids)))
            ).scalars()
        }
        if work_ids
        else {}
    )
    actor_ids = {n.actor_id for n in site.values() if n.actor_id}
    for n in v2.values():
        ann = (
            anns.get(n.event_source_id) if n.event_type == "work_announcement" else None
        )
        if ann and ann.author_id:
            actor_ids.add(ann.author_id)
    for r in rows:
        if r.src == "v" and r.category == "followed_author_new_work":
            w = works.get(r.work_id)
            if w and w.author_id:
                actor_ids.add(w.author_id)
    users = await load_users(session, actor_ids)

    items = []
    for r in rows:
        if r.src == "n":
            n = site[r.id]
            item = {
                "id": f"n{n.id}",
                "category": n.category,
                "actor": users.get(n.actor_id) if n.actor_id else None,
                "target": {
                    "type": str((n.target or {}).get("type") or "none"),
                    "id": str((n.target or {}).get("id") or ""),
                    "title": str((n.target or {}).get("title") or ""),
                },
                "excerpt": n.excerpt,
                "createdAt": iso_utc(n.created_at),
                "read": n.read_at is not None,
            }
            if n.context:
                item["context"] = n.context
            if n.mention:
                item["mention"] = n.mention
            items.append(item)
            continue
        n = v2[r.id]
        work = works.get(r.work_id)
        title = work.title if work else ""
        actor = None
        if n.event_type == "work_announcement":
            ann = anns.get(n.event_source_id)
            if ann is None or ann.withdrawn_at is not None:
                excerpt = "公告已撤回"
            else:
                excerpt = f"新公告：{ann.title}" if ann.title else ann.body[:100]
                actor = users.get(ann.author_id) if ann.author_id else None
        elif n.event_type == "thread_update":
            upd = upds.get(n.event_source_id)
            excerpt = (
                f"发布了新版本 {upd.version}：{upd.description[:80]}"
                if upd and upd.version
                else (upd.description[:100] if upd else "作品有更新")
            )
        else:
            excerpt = "关注的作者发布了新作品"
            actor = users.get(work.author_id) if work and work.author_id else None
            if actor:
                excerpt = f"{actor['displayName']} 发布了新作品"
        items.append(
            {
                "id": f"v{n.id}",
                "category": r.category,
                "actor": actor,
                "target": {"type": "work", "id": str(r.work_id), "title": title},
                "excerpt": excerpt,
                "createdAt": iso_utc(n.created_at),
                "read": n.read_at is not None,
                "context": {"workId": str(r.work_id)},
            }
        )
    return items


async def unread_counts(session, ctx) -> dict:
    u, conds = await union_query(session, ctx)
    rows = (
        await session.execute(
            select(u.c.category, func.count())
            .where(*conds, u.c.read_at.is_(None))
            .group_by(u.c.category)
        )
    ).all()
    categories = {c: int(n) for c, n in rows}
    return {"total": sum(categories.values()), "categories": categories}


def parse_ids(raw) -> tuple[list[int], list[int]] | None:
    if not isinstance(raw, list):
        return None
    site, v2 = [], []
    for value in raw:
        if not isinstance(value, str) or len(value) < 2 or not value[1:].isdigit():
            continue
        (site if value[0] == "n" else v2 if value[0] == "v" else []).append(
            int(value[1:])
        )
    return site, v2


async def mark_read(session, ctx, body: dict) -> None:
    """{ids} 或 {category, before}；只带 before 或什么都不带 = 全部已读。只动当前模式看得到的。"""
    u, conds = await union_query(session, ctx)
    conds.append(u.c.read_at.is_(None))
    if "ids" in body:
        parsed = parse_ids(body.get("ids"))
        if parsed is None:
            raise invalid({"ids": "要是提醒 ID 列表"})
        site, v2 = parsed
        conds.append(
            or_(
                and_(u.c.src == "n", u.c.id.in_(site or [-1])),
                and_(u.c.src == "v", u.c.id.in_(v2 or [-1])),
            )
        )
    else:
        if body.get("category") is not None:
            categories = parse_categories(body.get("category"))
            if categories is None:
                raise invalid({"category": "分类不对"})
            if categories:
                conds.append(u.c.category.in_(categories))
        before = body.get("before")
        if before is not None:
            try:
                at = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
            except ValueError:
                raise invalid({"before": "时间格式不对"})
            if at.tzinfo is not None:
                from datetime import timezone

                at = at.astimezone(timezone.utc).replace(tzinfo=None)
            conds.append(u.c.created_at <= at)
    rows = (await session.execute(select(u.c.src, u.c.id).where(*conds))).all()
    now = utc_now()
    site_ids = [r.id for r in rows if r.src == "n"]
    v2_ids = [r.id for r in rows if r.src == "v"]
    if site_ids:
        await session.execute(
            update(SiteNotification)
            .where(SiteNotification.id.in_(site_ids))
            .values(read_at=now)
        )
    if v2_ids:
        # 和 V1/V2 共用已读事实（notification.read_at）
        await session.execute(
            update(Notification).where(Notification.id.in_(v2_ids)).values(read_at=now)
        )


def add_notification(
    session,
    *,
    user_id: str,
    category: str,
    actor_id: str | None,
    target: dict,
    excerpt: str,
    work_id: int | None = None,
    context: dict | None = None,
) -> None:
    """给某人记一条站内提醒（自己触发的不记）。"""
    if actor_id and actor_id == user_id:
        return
    session.add(
        SiteNotification(
            user_id=user_id,
            category=category,
            actor_id=actor_id,
            target=target,
            excerpt=excerpt,
            work_id=work_id,
            context=context,
        )
    )


def create_notification(session, recipient_id: str, notification: dict) -> None:
    """给其他块（聊天的 @ 提醒、回复等）用：按协议 Notification 形状记一条站内提醒。

    - 只 session.add，不 commit（和调用方同一事务）；
    - notification：{category, actor（User 对象或 None）或 actorId, target{type,id,title},
      excerpt, context?, mention?}；id、createdAt、read 由这里定；
    - 来源作品取 context.workId，没有时 target.type=work 取 target.id。受限作品的提醒照样记下，
      读列表、计数时按查看者当时的有效模式过滤（全年龄模式不返回、不计数）；
    - 自己触发的不记；分类不认识时按 system。
    """
    if not recipient_id or not isinstance(notification, dict):
        return
    actor = notification.get("actor")
    actor_id = notification.get("actorId") or (
        actor.get("id") if isinstance(actor, dict) else actor
    )
    actor_id = str(actor_id) if actor_id else None
    target = notification.get("target") or {}
    target = {
        "type": str(target.get("type") or "none"),
        "id": str(target.get("id") or ""),
        "title": str(target.get("title") or ""),
    }
    context = notification.get("context")
    context = context if isinstance(context, dict) and context else None
    work_raw = (context or {}).get("workId") or (
        target["id"] if target["type"] == "work" else None
    )
    work_id = (
        int(work_raw) if work_raw is not None and str(work_raw).isdigit() else None
    )
    category = notification.get("category")
    if category not in CATEGORIES:
        category = "system"
    if actor_id and actor_id == str(recipient_id):
        return
    mention = notification.get("mention")
    session.add(
        SiteNotification(
            user_id=str(recipient_id),
            category=category,
            actor_id=actor_id,
            target=target,
            excerpt=str(notification.get("excerpt") or "")[:500],
            work_id=work_id,
            context=context,
            mention=mention if isinstance(mention, dict) else None,
        )
    )
