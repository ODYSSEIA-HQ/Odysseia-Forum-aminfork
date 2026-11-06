"""活动（协议 3.22，草案 L）：规则对齐前端假后端 src/mock/activities.ts、handlers/events.ts。

- 状态由时间推出：draft、cancelled 是手动的；没到 submitStartAt 是 upcoming，收稿期间 open，
  截止后到公布结果前 judging，公布后 closed（截止时间以服务器为准）。
- 截止后第一次读到活动时，把还没锁定的 pending/accepted 条目锁到「截止那一刻作品的最新版本」。
- R18：audience=r18 的活动只对有效模式 r18 可见（站务、主办人也一样），否则 404；
  草稿只给主办人和站务；查看者看不到的分组整组不出现（含奖项）；
  受限作品的条目不返回、不计数、不留占位（作品可见性统一用 work_access）。
- 权限：站务 = events.manage（site.admin 视为全部）；主办人 = organizer_ids，只在本活动有效；
  主办人不能参加自己主办的活动；收 R18 作品的活动，主办人必须有 R18 资格。
- 服务函数只 flush，事务由路由在生成响应后统一提交（expire_on_commit=True，提交后不能再读行）。
- 公布结果时给挂了 decorRoleId 的奖项的获奖作者发装饰身份组（services/me_extra.grant_decoration）；
  Work.events 角标见 work_event_refs；活动资格里的 tag 对外写成「组:名字」（见 tag_label）。
- 没做：关注者/参赛者提醒。
"""

import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import select

from models.attachment import Attachment
from models.site_event import SiteEvent, SiteEventEntry, SiteEventFollow, SiteEventLog
from models.site_tag import SiteTag, SiteWorkTag
from models.user import User
from models.work import Work
from models.work_version import WorkVersion
from shared.time_utils import utc_now
from site_v1 import mode as modes
from site_v1.context import SiteContext, Viewer
from site_v1.dto import iso_utc, load_users
from site_v1.errors import SiteError, conflict, forbidden, invalid, not_found
from site_v1.paging import PageRequest, encode_cursor
from site_v1.services.attachments import attachment_dto
from site_v1.services.work_access import parse_id, restricted_ids, visible_to, work_rating
from site_v1.services.works import serialize_works

STATE_ORDER = {"open": 0, "upcoming": 1, "judging": 2, "closed": 3, "draft": 4, "cancelled": 5}
RATINGS = (modes.GENERAL, modes.R18, modes.R18G)
ENTRY_STATES = ("pending", "accepted", "rejected", "withdrawn", "disqualified")
MAX_ORGANIZERS = 5


# ---------------- 小工具 ----------------


def clean_text(value, max_len: int = 20000) -> str:
    """去掉控制字符（协议 1.3），保留换行和制表符。"""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ord(ch) >= 32 or ch in "\t\n\r")[:max_len]


def parse_time(raw) -> datetime | None:
    """ISO 8601 → naive UTC；不合法返回 None。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def reason_error(reason: str, message: str) -> SiteError:
    return SiteError(422, "validation_failed", message, {"reason": reason})


def deadline_error() -> SiteError:
    return reason_error("deadline_passed", "已经过了收稿截止时间")


def new_key(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


def is_staff(viewer: Viewer | None) -> bool:
    return viewer is not None and viewer.has_permission("events.manage")


def is_organizer(e: SiteEvent, viewer: Viewer | None) -> bool:
    return viewer is not None and viewer.user_id in (e.organizer_ids or [])


def can_manage(e: SiteEvent, viewer: Viewer | None) -> bool:
    return is_staff(viewer) or is_organizer(e, viewer)


def event_state(e: SiteEvent, at: datetime | None = None) -> str:
    at = at or utc_now()
    if e.manual_state == "cancelled":
        return "cancelled"
    if e.manual_state == "draft":
        return "draft"
    if e.results_published_at:
        return "closed"
    if at < e.submit_start_at:
        return "upcoming"
    if at < e.submit_end_at:
        return "open"
    return "judging"


def deadline_passed(e: SiteEvent, at: datetime | None = None) -> bool:
    return (at or utc_now()) >= e.submit_end_at


def event_visible(e: SiteEvent | None, ctx: SiteContext) -> bool:
    if e is None:
        return False
    if e.audience == "r18" and ctx.effective_mode != modes.R18:
        return False
    if e.manual_state == "draft" and not can_manage(e, ctx.viewer):
        return False
    return True


def division_visible(d: dict, ctx: SiteContext) -> bool:
    allowed = ctx.visible_ratings()
    return all(r in allowed for r in d.get("ratings") or [])


def visible_divisions(e: SiteEvent, ctx: SiteContext) -> list[dict]:
    return [d for d in (e.divisions or []) if division_visible(d, ctx)]


def division_for(e: SiteEvent, rating: str) -> dict | None:
    return next((d for d in e.divisions or [] if rating in (d.get("ratings") or [])), None)


def award_by_id(e: SiteEvent, award_id: str | None) -> dict | None:
    return next((a for a in e.awards or [] if a.get("id") == award_id), None) if award_id else None


def eligibility_dto(e: SiteEvent) -> dict:
    el = e.eligibility or {}
    return {
        "resourceTypes": list(el.get("resourceTypes") or []),
        "requiredTags": list(el.get("requiredTags") or []),
        "ratings": list(el.get("ratings") or []),
        "newWorksSince": el.get("newWorksSince"),
        "maxEntriesPerUser": int(el.get("maxEntriesPerUser") or 1),
        "allowNewcomers": el.get("allowNewcomers") is not False,
    }


def tag_label(tag: SiteTag) -> str:
    """活动资格里 tag 的对外写法「组:名字」，和前端假后端的 tag ID（如 type:角色卡）一致。

    前端活动页按 `id.split(":").pop()` 显示名字，只给数字 ID 时会显示成数字。
    """
    return f"{tag.group_id}:{tag.name}"


async def eligibility_labels(session, events: list[SiteEvent]) -> dict[str, str]:
    """活动资格里存的 tag ID（数字字符串）→「组:名字」。"""
    raw = {
        str(t)
        for e in events
        for key in ("resourceTypes", "requiredTags")
        for t in ((e.eligibility or {}).get(key) or [])
    }
    ids = [int(t) for t in raw if t.isdigit()]
    if not ids:
        return {}
    rows = (await session.execute(select(SiteTag).where(SiteTag.id.in_(ids)))).scalars()
    return {str(t.id): tag_label(t) for t in rows}


def eligibility_out(e: SiteEvent, labels: dict[str, str]) -> dict:
    """对外的 EventEligibility：tag 写成「组:名字」（找不到的原样给）。"""
    el = eligibility_dto(e)
    for key in ("resourceTypes", "requiredTags"):
        el[key] = [labels.get(str(t), str(t)) for t in el[key]]
    return el


async def normalize_tag_refs(session, raw, field_name: str, errors: dict) -> list[str]:
    """管理端传来的资格 tag：接受数字 ID 和「组:名字」两种写法，统一存成数字 ID 字符串。"""
    out: list[str] = []
    for item in raw or []:
        value = str(item).strip()
        if not value:
            continue
        tag = None
        if value.isdigit():
            tag = await session.get(SiteTag, int(value))
        elif ":" in value:
            group, name = value.split(":", 1)
            tag = (
                await session.execute(
                    select(SiteTag).where(
                        SiteTag.group_id == group, SiteTag.name == name, SiteTag.deleted_at.is_(None)
                    )
                )
            ).scalars().first()
        if tag is None or tag.deleted_at is not None:
            errors["eligibility"] = f"{field_name} 里有不存在的 tag：{value}"
            continue
        if str(tag.id) not in out:
            out.append(str(tag.id))
    return out


def timeline_dto(e: SiteEvent) -> dict:
    return {
        "submitStartAt": iso_utc(e.submit_start_at),
        "submitEndAt": iso_utc(e.submit_end_at),
        "resultAt": iso_utc(e.result_at),
    }


def slice_page(items: list, page: PageRequest) -> tuple[list, str | None]:
    """内存分页（offset 游标）；活动、条目数量都不大。"""
    chunk = items[page.offset : page.offset + page.limit]
    more = page.offset + page.limit < len(items)
    return chunk, encode_cursor({"o": page.offset + len(chunk)}) if more else None


# ---------------- 读取 ----------------


async def find_event(session, raw_id, *, for_update: bool = False) -> SiteEvent | None:
    """按 ID 或 slug 找活动。"""
    event_id = parse_id(raw_id)
    q = select(SiteEvent)
    if event_id:
        q = q.where(SiteEvent.id == event_id)
    elif isinstance(raw_id, str) and raw_id:
        q = q.where(SiteEvent.slug == raw_id)
    else:
        return None
    if for_update:
        q = q.with_for_update()
    return (await session.execute(q)).scalar_one_or_none()


async def visible_event(session, ctx: SiteContext, raw_id, *, for_update=False) -> SiteEvent:
    e = await find_event(session, raw_id, for_update=for_update)
    if not event_visible(e, ctx):
        raise not_found()
    await settle(session, e)
    return e


async def managed_event(session, ctx: SiteContext, raw_id, *, for_update=False) -> SiteEvent:
    viewer = ctx.require_user()
    e = await visible_event(session, ctx, raw_id, for_update=for_update)
    if not can_manage(e, viewer):
        raise forbidden("只有主办人和站务可以管理这个活动")
    return e


def require_staff(ctx: SiteContext) -> Viewer:
    viewer = ctx.require_user()
    if not is_staff(viewer):
        raise forbidden("需要活动管理权限（events.manage）")
    return viewer


async def settle(session, e: SiteEvent) -> None:
    """截止后锁定参赛版本（截止那一刻作品的最新版本；截止前没有版本就取现在的最新版本）。"""
    if not deadline_passed(e):
        return
    entries = (
        await session.execute(
            select(SiteEventEntry).where(
                SiteEventEntry.event_id == e.id,
                SiteEventEntry.version_id.is_(None),
                SiteEventEntry.state.in_(("pending", "accepted")),
            )
        )
    ).scalars().all()
    if not entries:
        return
    work_ids = list({en.work_id for en in entries})
    versions = (
        await session.execute(
            select(WorkVersion.work_id, WorkVersion.id, WorkVersion.created_at)
            .where(WorkVersion.work_id.in_(work_ids))
            .order_by(WorkVersion.created_at.desc(), WorkVersion.id.desc())
        )
    ).all()
    before: dict[int, int] = {}
    latest: dict[int, int] = {}
    for work_id, version_id, created_at in versions:
        latest.setdefault(work_id, version_id)
        if created_at <= e.submit_end_at:
            before.setdefault(work_id, version_id)
    changed = False
    for en in entries:
        version = before.get(en.work_id) or latest.get(en.work_id)
        if version:
            en.version_id = version
            changed = True
    if changed:
        await session.flush()


async def load_works(session, ids) -> dict[int, Work]:
    ids = list({int(i) for i in ids if i})
    if not ids:
        return {}
    return {w.id: w for w in (await session.execute(select(Work).where(Work.id.in_(ids)))).scalars()}


async def visible_work_ids(session, ctx: SiteContext, works: dict[int, Work]) -> set[int]:
    restricted = await restricted_ids(session, list(works))
    return {wid for wid, w in works.items() if visible_to(ctx, w, wid in restricted)}


async def public_entries_by_event(
    session, ctx: SiteContext, events: list[SiteEvent]
) -> dict[int, list[SiteEventEntry]]:
    """公开条目：accepted、分组可见、作品对查看者可见（受限作品不返回、不计数）。"""
    out: dict[int, list[SiteEventEntry]] = {e.id: [] for e in events}
    if not events:
        return out
    rows = (
        await session.execute(
            select(SiteEventEntry).where(
                SiteEventEntry.event_id.in_(list(out)), SiteEventEntry.state == "accepted"
            )
        )
    ).scalars().all()
    works = await load_works(session, [r.work_id for r in rows])
    visible = await visible_work_ids(session, ctx, works)
    divisions = {e.id: {d["id"] for d in visible_divisions(e, ctx)} for e in events}
    for r in rows:
        if r.division_id in divisions[r.event_id] and r.work_id in visible:
            out[r.event_id].append(r)
    return out


async def event_dtos(session, ctx: SiteContext, events: list[SiteEvent]) -> list[dict]:
    """批量序列化协议 Event。"""
    if not events:
        return []
    viewer = ctx.viewer
    public = await public_entries_by_event(session, ctx, events)
    labels = await eligibility_labels(session, events)
    organizer_ids = {uid for e in events for uid in (e.organizer_ids or [])}
    users = await load_users(session, organizer_ids)
    covers = {
        a.id: a
        for a in (
            await session.execute(
                select(Attachment).where(
                    Attachment.id.in_([e.cover_attachment_id for e in events if e.cover_attachment_id] or [0]),
                    Attachment.deleted_at.is_(None),
                )
            )
        ).scalars()
    }
    following: set[int] = set()
    mine: dict[int, list[SiteEventEntry]] = {}
    if viewer is not None:
        ids = [e.id for e in events]
        following = set(
            (
                await session.execute(
                    select(SiteEventFollow.event_id).where(
                        SiteEventFollow.user_id == viewer.user_id,
                        SiteEventFollow.event_id.in_(ids),
                    )
                )
            ).scalars()
        )
        for en in (
            await session.execute(
                select(SiteEventEntry)
                .where(
                    SiteEventEntry.submitted_by == viewer.user_id,
                    SiteEventEntry.event_id.in_(ids),
                )
                .order_by(SiteEventEntry.id)
            )
        ).scalars():
            mine.setdefault(en.event_id, []).append(en)
    out = []
    for e in events:
        divisions = visible_divisions(e, ctx)
        division_ids = {d["id"] for d in divisions}
        cover = covers.get(e.cover_attachment_id) if e.cover_attachment_id else None
        my_entries = mine.get(e.id, [])
        reason = cannot_submit_reason(e, viewer, my_entries)
        out.append(
            {
                "id": str(e.id),
                "slug": e.slug,
                "title": e.title,
                "summary": e.summary or "",
                "body": e.body or "",
                "rules": e.rules or "",
                # 活动封面必须是全年龄图；不是的话 attachment_dto 对全年龄模式给占位
                "cover": attachment_dto(cover, ctx) if cover else None,
                "audience": e.audience,
                "state": event_state(e),
                "timeline": timeline_dto(e),
                "eligibility": eligibility_out(e, labels),
                "divisions": [
                    {"id": d["id"], "name": d.get("name", ""), "ratings": list(d.get("ratings") or [])}
                    for d in divisions
                ],
                "awards": [
                    award_dto(a)
                    for a in sorted(e.awards or [], key=lambda a: a.get("order", 0))
                    if a.get("divisionId") in division_ids
                ],
                "judging": "organizer",
                "organizers": [users[u] for u in (e.organizer_ids or []) if u in users],
                "entryCount": len(public.get(e.id, [])),
                "resultsPublished": e.results_published_at is not None,
                "createdAt": iso_utc(e.created_at),
                "updatedAt": iso_utc(e.updated_at),
                "viewer": {
                    "following": e.id in following,
                    "canSubmit": reason is None,
                    "cannotSubmitReason": reason,
                    "canManage": can_manage(e, viewer),
                    "myEntryIds": [str(en.id) for en in my_entries],
                },
            }
        )
    return out


async def event_dto(session, ctx: SiteContext, e: SiteEvent) -> dict:
    return (await event_dtos(session, ctx, [e]))[0]


def award_dto(a: dict) -> dict:
    return {
        "id": a["id"],
        "divisionId": a.get("divisionId"),
        "name": a.get("name", ""),
        "description": a.get("description", ""),
        "order": int(a.get("order", 0)),
        "decorRoleId": a.get("decorRoleId"),
    }


def active_count(entries: list[SiteEventEntry]) -> int:
    return sum(1 for en in entries if en.state != "withdrawn")


def cannot_submit_reason(e: SiteEvent, viewer: Viewer | None, my_entries: list[SiteEventEntry]) -> str | None:
    if event_state(e) != "open":
        return "not_open"
    if viewer is None:
        return "login_required"
    if is_organizer(e, viewer):
        return "organizer"
    if viewer.is_newcomer and not eligibility_dto(e)["allowNewcomers"]:
        return "newcomer"
    if active_count(my_entries) >= eligibility_dto(e)["maxEntriesPerUser"]:
        return "limit_reached"
    return None


async def entry_dtos(session, ctx: SiteContext, e_by_id: dict[int, SiteEvent], entries: list[SiteEventEntry]) -> list[dict]:
    """批量序列化 EventEntry（调用方保证作品对查看者可见）。"""
    if not entries:
        return []
    works = await load_works(session, [en.work_id for en in entries])
    serialized = {
        int(w["id"]): w for w in await serialize_works(session, ctx, list(works.values()))
    }
    users = await load_users(session, [en.submitted_by for en in entries])
    version_ids = [en.version_id for en in entries if en.version_id]
    names = {}
    if version_ids:
        names = dict(
            (
                await session.execute(
                    select(WorkVersion.id, WorkVersion.name).where(WorkVersion.id.in_(version_ids))
                )
            ).all()
        )
    from site_v1.services.works import unknown_user

    out = []
    for en in entries:
        e = e_by_id[en.event_id]
        award = award_by_id(e, en.award_id) if e.results_published_at else None
        out.append(
            {
                "id": str(en.id),
                "eventId": str(en.event_id),
                "divisionId": en.division_id,
                "work": serialized.get(en.work_id),
                "submittedBy": users.get(en.submitted_by) or unknown_user(en.submitted_by),
                "note": en.note or "",
                "versionId": str(en.version_id) if en.version_id else None,
                "versionName": names.get(en.version_id) if en.version_id else None,
                "state": en.state,
                "stateReason": en.state_reason,
                "award": {"id": award["id"], "name": award.get("name", "")} if award else None,
                "submittedAt": iso_utc(en.submitted_at),
            }
        )
    return [o for o in out if o["work"] is not None]


async def work_event_refs(session, ctx: SiteContext, work_ids: list[int]) -> dict[int, list[dict]]:
    """Work.events：作品参加的活动（参赛、获奖角标），对齐假后端 workEventRefs。

    只列 accepted 的条目；取消的活动、查看者看不到的活动（R18 活动、别人的草稿）和分组不出现；
    结果公布后才带 award。作品本身的可见性由调用方（serialize_works）保证。
    """
    out: dict[int, list[dict]] = {}
    if not work_ids:
        return out
    rows = (
        await session.execute(
            select(SiteEventEntry, SiteEvent)
            .join(SiteEvent, SiteEvent.id == SiteEventEntry.event_id)
            .where(
                SiteEventEntry.work_id.in_(list(work_ids)),
                SiteEventEntry.state == "accepted",
                SiteEvent.manual_state != "cancelled",
            )
            .order_by(SiteEventEntry.submitted_at, SiteEventEntry.id)
        )
    ).all()
    for en, e in rows:
        if not event_visible(e, ctx):
            continue
        division = next((d for d in e.divisions or [] if d.get("id") == en.division_id), None)
        if division is None or not division_visible(division, ctx):
            continue
        award = award_by_id(e, en.award_id) if e.results_published_at else None
        out.setdefault(en.work_id, []).append(
            {
                "eventId": str(e.id),
                "title": e.title,
                "entryId": str(en.id),
                "award": {"id": award["id"], "name": award.get("name", "")} if award else None,
            }
        )
    return out


async def add_log(session, e: SiteEvent, actor: Viewer | None, action: str, detail: str, work_id: int | None = None) -> None:
    session.add(
        SiteEventLog(
            event_id=e.id,
            actor_id=actor.user_id if actor else None,
            action=action,
            detail=detail,
            work_id=work_id,
        )
    )
    e.updated_at = utc_now()


# ---------------- 浏览 ----------------


async def list_events(session, ctx: SiteContext, *, state: str | None, q: str | None, page: PageRequest) -> dict:
    states = [s for s in (state or "").split(",") if s]
    keyword = (q or "").strip().lower()
    rows = (await session.execute(select(SiteEvent))).scalars().all()
    events = [e for e in rows if event_visible(e, ctx)]
    if states:
        events = [e for e in events if event_state(e) in states]
    if keyword:
        events = [e for e in events if keyword in f"{e.title} {e.summary}".lower()]

    def sort_key(e: SiteEvent):
        rank = STATE_ORDER[event_state(e)]
        ts = e.submit_end_at.timestamp()
        # 进行中、即将开始的按截止时间升序；已结束的按截止时间降序
        return (rank, -ts if rank >= 2 else ts, e.id)

    events.sort(key=sort_key)
    chunk, cursor = slice_page(events, page)
    for e in chunk:
        await settle(session, e)
    return {"items": await event_dtos(session, ctx, chunk), "nextCursor": cursor}


def _seed_score(seed: str, entry_id: int) -> int:
    h = 0
    for ch in seed + str(entry_id):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


async def list_entries(session, ctx: SiteContext, raw_id, *, division, sort, seed, page) -> dict:
    e = await visible_event(session, ctx, raw_id)
    if division:
        d = next((x for x in e.divisions or [] if x.get("id") == division), None)
        if d is None or not division_visible(d, ctx):
            raise not_found()
    entries = (await public_entries_by_event(session, ctx, [e]))[e.id]
    if division:
        entries = [en for en in entries if en.division_id == division]
    sort = sort or "new"
    if sort == "random":
        entries.sort(key=lambda en: _seed_score(seed or "0", en.id))
    elif sort == "award" and e.results_published_at:
        def order(en):
            a = award_by_id(e, en.award_id)
            return a.get("order", 999) if a else 999

        entries.sort(key=lambda en: (order(en), -en.submitted_at.timestamp()))
    else:
        entries.sort(key=lambda en: (en.submitted_at, en.id), reverse=True)
    chunk, cursor = slice_page(entries, page)
    return {"items": await entry_dtos(session, ctx, {e.id: e}, chunk), "nextCursor": cursor}


async def results(session, ctx: SiteContext, raw_id) -> dict:
    e = await visible_event(session, ctx, raw_id)
    if not e.results_published_at:
        raise not_found("结果还没公布")
    winners = (
        await session.execute(
            select(SiteEventEntry).where(
                SiteEventEntry.event_id == e.id,
                SiteEventEntry.state == "accepted",
                SiteEventEntry.award_id.is_not(None),
            ).order_by(SiteEventEntry.id)
        )
    ).scalars().all()
    works = await load_works(session, [w.work_id for w in winners])
    visible = await visible_work_ids(session, ctx, works)
    serialized = {
        int(w["id"]): w
        for w in await serialize_works(session, ctx, [works[i] for i in visible])
    }
    divisions = []
    for d in visible_divisions(e, ctx):
        awards = []
        for a in sorted((x for x in e.awards or [] if x.get("divisionId") == d["id"]), key=lambda x: x.get("order", 0)):
            ws = [
                {"entryId": str(en.id), "work": serialized[en.work_id], "note": en.award_note or ""}
                for en in winners
                if en.award_id == a["id"] and en.work_id in serialized
            ]
            # 没有可见获奖作品的奖项不出现
            if ws:
                awards.append({"id": a["id"], "name": a.get("name", ""), "description": a.get("description", ""), "winners": ws})
        if awards:
            divisions.append({"id": d["id"], "name": d.get("name", ""), "awards": awards})
    return {"publishedAt": iso_utc(e.results_published_at), "divisions": divisions}


async def set_follow(session, ctx: SiteContext, raw_id, on: bool) -> None:
    viewer = ctx.require_user()
    e = await visible_event(session, ctx, raw_id)
    row = await session.get(SiteEventFollow, (e.id, viewer.user_id))
    if on and row is None:
        session.add(SiteEventFollow(event_id=e.id, user_id=viewer.user_id))
    elif not on and row is not None:
        await session.delete(row)
    await session.flush()


# ---------------- 参赛 ----------------


async def ineligible_reason(session, e: SiteEvent, work: Work, viewer: Viewer) -> str | None:
    el = eligibility_dto(e)
    if work.author_id != viewer.user_id:
        return "只能提交自己的作品"
    if work.status != "published":
        return "作品还没发布"
    # 作品上的 tag：数字 ID 和「组:名字」两种写法都认（旧数据里可能存的是后一种）
    tag_ids: set[str] = set()
    for tag in (
        await session.execute(
            select(SiteTag)
            .join(SiteWorkTag, SiteWorkTag.tag_id == SiteTag.id)
            .where(SiteWorkTag.work_id == work.id, SiteWorkTag.removed_at.is_(None))
        )
    ).scalars():
        tag_ids.update((str(tag.id), tag_label(tag)))
    if el["resourceTypes"] and not any(str(t) in tag_ids for t in el["resourceTypes"]):
        return "资源类型不符合"
    missing = [str(t) for t in el["requiredTags"] if str(t) not in tag_ids]
    if missing:
        return "缺少必带 tag"
    if work_rating(work) not in el["ratings"]:
        return "这个活动不收这个分级的作品"
    since = parse_time(el["newWorksSince"])
    if since and (work.published_at or work.created_at) < since:
        return "只收活动开始后发布的新作"
    dup = (
        await session.execute(
            select(SiteEventEntry.id).where(
                SiteEventEntry.event_id == e.id,
                SiteEventEntry.work_id == work.id,
                SiteEventEntry.state != "withdrawn",
            )
        )
    ).first()
    if dup:
        return "已经提交过了"
    return None


async def submit_entry(session, ctx: SiteContext, raw_id, body: dict) -> dict:
    viewer = ctx.require_user()
    # 锁活动行：同一个人并发提交时「每人几件」不会被绕过
    e = await visible_event(session, ctx, raw_id, for_update=True)
    state = event_state(e)
    if state != "open":
        if deadline_passed(e):
            raise deadline_error()
        raise reason_error("not_open", "活动还没开始收稿")
    if is_organizer(e, viewer):
        raise forbidden("主办人不能参加自己主办的活动")
    el = eligibility_dto(e)
    if viewer.is_newcomer and not el["allowNewcomers"]:
        raise SiteError(403, "onboarding_required", "新人缓冲期账号不能参加这个活动", {"until": None})
    work_id = parse_id(body.get("workId"))
    work = await session.get(Work, work_id) if work_id else None
    if work is None or not visible_to(ctx, work, bool(await restricted_ids(session, [work.id]))):
        raise invalid({"workId": "找不到这个作品"})
    if work.author_id != viewer.user_id:
        raise forbidden("只能提交自己的作品")
    mine = (
        await session.execute(
            select(SiteEventEntry).where(
                SiteEventEntry.event_id == e.id, SiteEventEntry.submitted_by == viewer.user_id
            )
        )
    ).scalars().all()
    if active_count(mine) >= el["maxEntriesPerUser"]:
        raise reason_error("limit_reached", f"每人最多提交 {el['maxEntriesPerUser']} 件")
    reason = await ineligible_reason(session, e, work, viewer)
    if reason:
        raise reason_error("already_submitted" if reason == "已经提交过了" else "not_eligible", reason)
    division = division_for(e, work_rating(work))
    if division is None:
        raise reason_error("not_eligible", "这个活动不收这个分级的作品")
    entry = SiteEventEntry(
        event_id=e.id,
        division_id=division["id"],
        work_id=work.id,
        submitted_by=viewer.user_id,
        note=clean_text(body.get("note"), 2000),
        # 提交后直接显示（E3），主办人事后可以拒绝或取消资格
        state="accepted",
    )
    session.add(entry)
    await add_log(session, e, viewer, "submit", f"提交「{work.title}」", work.id)
    await session.flush()
    return (await entry_dtos(session, ctx, {e.id: e}, [entry]))[0]


async def own_entry(session, ctx: SiteContext, raw_id, entry_id) -> tuple[SiteEvent, SiteEventEntry]:
    viewer = ctx.require_user()
    e = await visible_event(session, ctx, raw_id)
    en = await session.get(SiteEventEntry, parse_id(entry_id) or 0)
    if en is None or en.event_id != e.id or en.submitted_by != viewer.user_id:
        raise not_found()
    return e, en


async def update_entry(session, ctx: SiteContext, raw_id, entry_id, body: dict) -> dict:
    e, en = await own_entry(session, ctx, raw_id, entry_id)
    if deadline_passed(e):
        raise deadline_error()
    if isinstance(body.get("note"), str):
        en.note = clean_text(body["note"], 2000)
    await session.flush()
    dtos = await entry_dtos(session, ctx, {e.id: e}, [en])
    if not dtos:
        raise not_found()
    return dtos[0]


async def withdraw_entry(session, ctx: SiteContext, raw_id, entry_id) -> None:
    e, en = await own_entry(session, ctx, raw_id, entry_id)
    if deadline_passed(e):
        raise deadline_error()
    if en.state != "withdrawn":
        en.state = "withdrawn"
        await add_log(session, e, ctx.viewer, "withdraw", "作者撤回条目", en.work_id)
    await session.flush()


async def my_entries(session, ctx: SiteContext, page: PageRequest) -> dict:
    viewer = ctx.require_user()
    entries = (
        await session.execute(
            select(SiteEventEntry)
            .where(SiteEventEntry.submitted_by == viewer.user_id)
            .order_by(SiteEventEntry.submitted_at.desc(), SiteEventEntry.id.desc())
        )
    ).scalars().all()
    events = {
        e.id: e
        for e in (
            await session.execute(
                select(SiteEvent).where(SiteEvent.id.in_(list({en.event_id for en in entries}) or [0]))
            )
        ).scalars()
    }
    works = await load_works(session, [en.work_id for en in entries])
    visible = await visible_work_ids(session, ctx, works)
    # 受限作品的条目在全年龄模式下不出现；活动本身看不到的也不出现
    entries = [
        en for en in entries if event_visible(events.get(en.event_id), ctx) and en.work_id in visible
    ]
    chunk, cursor = slice_page(entries, page)
    for eid in {en.event_id for en in chunk}:
        await settle(session, events[eid])
    dtos = await entry_dtos(session, ctx, events, chunk)
    covers = {
        a.id: a
        for a in (
            await session.execute(
                select(Attachment).where(
                    Attachment.id.in_([e.cover_attachment_id for e in events.values() if e.cover_attachment_id] or [0])
                )
            )
        ).scalars()
    }
    for dto in dtos:
        e = events[int(dto["eventId"])]
        cover = covers.get(e.cover_attachment_id) if e.cover_attachment_id else None
        dto["event"] = {
            "id": str(e.id),
            "title": e.title,
            "state": event_state(e),
            "timeline": timeline_dto(e),
            "cover": attachment_dto(cover, ctx) if cover else None,
        }
    return {"items": dtos, "nextCursor": cursor}


# ---------------- 主办人 ----------------


async def resolve_cover(session, viewer: Viewer, raw) -> int | None:
    """封面上传：必须是 ready 的全年龄图片（活动封面对所有人展示）。"""
    if raw is None:
        return None
    att = await session.get(Attachment, parse_id(raw) or 0)
    if att is None or att.deleted_at is not None or att.kind != "image" or att.state != "ready":
        raise invalid({"coverUploadId": "找不到这张图片，或还没处理完"})
    if att.owner_id != viewer.user_id and not is_staff(viewer):
        raise invalid({"coverUploadId": "只能用自己上传的图片"})
    if (att.rating or modes.GENERAL) != modes.GENERAL:
        raise invalid({"coverUploadId": "活动封面必须是全年龄图片"})
    return att.id


async def patch_event(session, ctx: SiteContext, raw_id, body: dict) -> dict:
    e = await managed_event(session, ctx, raw_id, for_update=True)
    viewer = ctx.viewer
    fields = []
    if isinstance(body.get("summary"), str):
        e.summary = clean_text(body["summary"], 300)
        fields.append("简介")
    if isinstance(body.get("body"), str):
        e.body = clean_text(body["body"])
        fields.append("介绍")
    if isinstance(body.get("rules"), str):
        e.rules = clean_text(body["rules"])
        fields.append("规则")
    if "coverUploadId" in body:
        e.cover_attachment_id = await resolve_cover(session, viewer, body.get("coverUploadId"))
        fields.append("封面")
    await add_log(session, e, viewer, "edit", f"修改了{'、'.join(fields) or '活动'}")
    await session.flush()
    return await event_dto(session, ctx, e)


async def manage_entries(session, ctx: SiteContext, raw_id, *, state: str | None, page: PageRequest) -> dict:
    e = await managed_event(session, ctx, raw_id)
    states = [s for s in (state or "").split(",") if s]
    q = select(SiteEventEntry).where(SiteEventEntry.event_id == e.id)
    if states:
        q = q.where(SiteEventEntry.state.in_(states))
    entries = (
        await session.execute(q.order_by(SiteEventEntry.submitted_at.desc(), SiteEventEntry.id.desc()))
    ).scalars().all()
    works = await load_works(session, [en.work_id for en in entries])
    visible = await visible_work_ids(session, ctx, works)
    divisions = {d["id"] for d in visible_divisions(e, ctx)}
    # 仍按主办人自己的有效模式过滤
    entries = [en for en in entries if en.work_id in visible and en.division_id in divisions]
    chunk, cursor = slice_page(entries, page)
    return {"items": await entry_dtos(session, ctx, {e.id: e}, chunk), "nextCursor": cursor}


REVIEW_LABELS = {"accept": "通过", "restore": "恢复", "reject": "拒绝", "disqualify": "取消资格"}


async def review_entry(session, ctx: SiteContext, raw_id, entry_id, body: dict) -> dict:
    e = await managed_event(session, ctx, raw_id, for_update=True)
    en = await session.get(SiteEventEntry, parse_id(entry_id) or 0, with_for_update=True)
    if en is None or en.event_id != e.id:
        raise not_found()
    works = await load_works(session, [en.work_id])
    if en.work_id not in await visible_work_ids(session, ctx, works):
        raise not_found()
    decision = body.get("decision")
    reason = clean_text(body.get("reason"), 500).strip()
    if decision not in REVIEW_LABELS:
        raise invalid({"decision": "只能是 accept、reject、disqualify、restore"})
    if decision in ("reject", "disqualify") and not reason:
        raise invalid({"reason": "拒绝和取消资格必须写理由"})
    if decision in ("accept", "restore"):
        en.state, en.state_reason = "accepted", None
    elif decision == "reject":
        en.state, en.state_reason = "rejected", reason
    else:
        en.state, en.state_reason, en.award_id = "disqualified", reason, None
    await add_log(
        session, e, ctx.viewer, "review",
        f"{REVIEW_LABELS[decision]}条目{'：' + reason if reason else ''}", en.work_id,
    )
    await session.flush()
    return (await entry_dtos(session, ctx, {e.id: e}, [en]))[0]


async def save_results(session, ctx: SiteContext, raw_id, body: dict) -> dict:
    e = await managed_event(session, ctx, raw_id, for_update=True)
    awards = body.get("awards")
    if not isinstance(awards, list):
        raise invalid({"awards": "缺少 awards"})
    accepted = {
        en.id: en
        for en in (
            await session.execute(
                select(SiteEventEntry).where(
                    SiteEventEntry.event_id == e.id, SiteEventEntry.state == "accepted"
                )
            )
        ).scalars()
    }
    draft = []
    for a in awards:
        if not isinstance(a, dict):
            raise invalid({"awards": "格式不对"})
        award = award_by_id(e, a.get("awardId"))
        if award is None:
            raise invalid({"awards": f"没有这个奖项：{a.get('awardId')}"})
        entry_ids = a.get("entryIds") or []
        if not isinstance(entry_ids, list):
            raise invalid({"awards": "entryIds 要是数组"})
        for raw in entry_ids:
            en = accepted.get(parse_id(raw) or 0)
            if en is None:
                raise invalid({"awards": "只能给已通过的条目评奖"})
            if en.division_id != award.get("divisionId"):
                raise invalid({"awards": "奖项和条目不在同一个分组"})
        notes = a.get("notes") if isinstance(a.get("notes"), dict) else {}
        draft.append(
            {
                "awardId": award["id"],
                "entryIds": [str(x) for x in entry_ids],
                "notes": {str(k): clean_text(v, 1000) for k, v in notes.items()},
            }
        )
    e.results_draft = draft
    await add_log(session, e, ctx.viewer, "results", "保存结果草稿")
    await session.flush()
    return {"awards": draft}


async def get_results_draft(session, ctx: SiteContext, raw_id) -> dict:
    e = await managed_event(session, ctx, raw_id)
    return {"awards": list(e.results_draft or [])}


async def publish_results(session, ctx: SiteContext, raw_id) -> dict:
    viewer = require_staff(ctx)
    e = await visible_event(session, ctx, raw_id, for_update=True)
    if event_state(e) == "cancelled":
        raise reason_error("not_open", "活动已取消")
    if not deadline_passed(e):
        raise reason_error("not_open", "收稿还没截止，不能公布结果")
    entries = {
        en.id: en
        for en in (
            await session.execute(select(SiteEventEntry).where(SiteEventEntry.event_id == e.id))
        ).scalars()
    }
    for en in entries.values():
        en.award_id, en.award_note = None, ""
    winners = []
    for d in e.results_draft or []:
        award = award_by_id(e, d.get("awardId"))
        if award is None:
            continue
        for raw in d.get("entryIds") or []:
            en = entries.get(parse_id(raw) or 0)
            if en is None or en.state != "accepted":
                continue
            en.award_id = award["id"]
            en.award_note = (d.get("notes") or {}).get(str(raw), "")
            if award.get("decorRoleId"):
                winners.append((en.submitted_by, award))
    e.results_published_at = utc_now()
    # 获奖后自动发勋章（E9）：奖项挂了装饰身份组时发给获奖作者，操作人要有 roles.grant_decor
    # （同假后端）；发进 site_user_decoration_grant，和 /me/decorations、勋章墙是同一份数据
    detail = "公布结果"
    if winners:
        from site_v1.services.me_extra import grant_decoration

        names = await load_users(session, [u for u, _ in winners])
        if viewer.has_permission("roles.grant_decor"):
            medals: list[str] = []
            for user_id, award in winners:
                fresh = await grant_decoration(
                    session,
                    user_id,
                    str(award["decorRoleId"]),
                    f"活动：{e.title} {award.get('name', '')}",
                    f"{e.title}·{award.get('name', '')}",
                )
                if fresh and user_id in names and names[user_id]["displayName"] not in medals:
                    medals.append(names[user_id]["displayName"])
            if medals:
                detail += f"，发放勋章给 {'、'.join(medals)}"
        else:
            pending = dict.fromkeys(names[u]["displayName"] for u, _ in winners if u in names)
            detail += f"，没有 roles.grant_decor 权限，未发勋章：{'、'.join(pending)}"
    await add_log(session, e, viewer, "results_publish", detail)
    await session.flush()
    return await event_dto(session, ctx, e)


async def event_log(session, ctx: SiteContext, raw_id, page: PageRequest) -> dict:
    e = await managed_event(session, ctx, raw_id)
    logs = (
        await session.execute(
            select(SiteEventLog)
            .where(SiteEventLog.event_id == e.id)
            .order_by(SiteEventLog.at.desc(), SiteEventLog.id.desc())
        )
    ).scalars().all()
    works = await load_works(session, [row.work_id for row in logs if row.work_id])
    visible = await visible_work_ids(session, ctx, works)
    # 涉及受限作品的日志按查看者有效模式过滤
    logs = [row for row in logs if not row.work_id or row.work_id in visible]
    chunk, cursor = slice_page(logs, page)
    users = await load_users(session, [row.actor_id for row in chunk if row.actor_id])
    return {
        "items": [
            {
                "id": str(row.id),
                "actor": users.get(row.actor_id) if row.actor_id else None,
                "action": row.action,
                "detail": row.detail,
                "at": iso_utc(row.at),
            }
            for row in chunk
        ],
        "nextCursor": cursor,
    }


# ---------------- 站务 ----------------

SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


async def apply_admin_input(session, viewer: Viewer, e: SiteEvent, body: dict, creating: bool) -> None:
    errors: dict[str, str] = {}
    if "title" in body:
        e.title = clean_text(body.get("title"), 80).strip()
    if not e.title:
        errors["title"] = "请填写标题"
    if "slug" in body:
        slug = SLUG_RE.sub("-", clean_text(body.get("slug"), 60).strip()).strip("-")
        if slug and slug.isdigit():
            errors["slug"] = "短名不能是纯数字"
        elif slug:
            taken = (
                await session.execute(
                    select(SiteEvent.id).where(SiteEvent.slug == slug, SiteEvent.id != (e.id or 0))
                )
            ).first()
            if taken:
                errors["slug"] = "这个短名已经被用了"
            else:
                e.slug = slug
    for key, limit in (("summary", 300), ("body", 20000), ("rules", 20000)):
        if key in body:
            setattr(e, key, clean_text(body.get(key), limit))
    if "audience" in body:
        e.audience = "r18" if body.get("audience") == "r18" else "all"
    if "coverUploadId" in body:
        try:
            e.cover_attachment_id = await resolve_cover(session, viewer, body.get("coverUploadId"))
        except SiteError as exc:
            errors.update(exc.details.get("fields", {}))
    tl = body.get("timeline")
    if isinstance(tl, dict):
        start, end = parse_time(tl.get("submitStartAt")), parse_time(tl.get("submitEndAt"))
        result_at = parse_time(tl.get("resultAt")) if tl.get("resultAt") else None
        if start is None or end is None:
            errors["timeline"] = "时间格式不对"
        elif end <= start:
            errors["timeline"] = "截止时间要晚于开始时间"
        else:
            e.submit_start_at, e.submit_end_at, e.result_at = start, end, result_at
    el = body.get("eligibility")
    if isinstance(el, dict):
        ratings = [r for r in (el.get("ratings") or []) if r in RATINGS]
        if not ratings:
            errors["eligibility"] = "至少接受一种分级"
        try:
            max_entries = int(el.get("maxEntriesPerUser") or 1)
        except (TypeError, ValueError):
            max_entries = 1
        since = el.get("newWorksSince") or None
        if since and parse_time(since) is None:
            errors["eligibility"] = "newWorksSince 时间格式不对"
        e.eligibility = {
            "resourceTypes": await normalize_tag_refs(session, el.get("resourceTypes"), "resourceTypes", errors),
            "requiredTags": await normalize_tag_refs(session, el.get("requiredTags"), "requiredTags", errors),
            "ratings": ratings,
            "newWorksSince": iso_utc(parse_time(since)) if since and parse_time(since) else None,
            "maxEntriesPerUser": max(1, min(20, max_entries)),
            "allowNewcomers": el.get("allowNewcomers") is not False,
        }
    if isinstance(body.get("divisions"), list):
        e.divisions = [
            {
                "id": str(d.get("id") or new_key("ed")),
                "name": clean_text(d.get("name"), 40),
                "ratings": [r for r in (d.get("ratings") or []) if r in RATINGS],
            }
            for d in body["divisions"]
            if isinstance(d, dict)
        ]
    if isinstance(body.get("awards"), list):
        e.awards = [
            {
                "id": str(a.get("id") or new_key("ea")),
                "divisionId": str(a.get("divisionId") or ""),
                "name": clean_text(a.get("name"), 40),
                "description": clean_text(a.get("description"), 200),
                "order": int(a["order"]) if isinstance(a.get("order"), int) else i + 1,
                "decorRoleId": a.get("decorRoleId") or None,
            }
            for i, a in enumerate(body["awards"])
            if isinstance(a, dict)
        ]
    ratings = eligibility_dto(e)["ratings"]
    mixed = modes.GENERAL in ratings and any(r != modes.GENERAL for r in ratings)
    if creating and not isinstance(body.get("divisions"), list):
        if mixed:
            e.divisions = [
                {"id": new_key("ed"), "name": "全年龄组", "ratings": [modes.GENERAL]},
                {"id": new_key("ed"), "name": "R18 组", "ratings": [r for r in ratings if r != modes.GENERAL]},
            ]
        else:
            e.divisions = [
                {
                    "id": new_key("ed"),
                    "name": "全年龄组" if modes.GENERAL in ratings else "R18 组",
                    "ratings": list(ratings),
                }
            ]
    ids = [d["id"] for d in e.divisions or []]
    if len(set(ids)) != len(ids):
        errors["divisions"] = "分组 ID 重复"
    for d in e.divisions or []:
        if not d["ratings"]:
            errors["divisions"] = "每个分组至少收一种分级"
        if modes.GENERAL in d["ratings"] and any(r != modes.GENERAL for r in d["ratings"]):
            errors["divisions"] = "同时收全年龄和 R18 作品的活动必须按分级分组：全年龄和 R18 不能放在同一个组"
    if any(not any(r in d["ratings"] for d in e.divisions or []) for r in ratings):
        errors["divisions"] = "每个接受的分级都要有对应的分组"
    if modes.GENERAL not in ratings and e.audience != "r18":
        errors["audience"] = "只收 R18 作品的活动必须设成「仅 R18 模式可见」"
    for a in e.awards or []:
        if a["divisionId"] not in ids:
            errors["awards"] = "每个奖项都要属于一个分组"
    if errors:
        raise invalid(errors)


async def admin_create(session, ctx: SiteContext, body: dict) -> dict:
    viewer = require_staff(ctx)
    now = utc_now()
    from datetime import timedelta

    e = SiteEvent(
        slug=new_key("ev"),
        title="",
        audience="all",
        manual_state="draft",
        submit_start_at=now + timedelta(days=7),
        submit_end_at=now + timedelta(days=30),
        eligibility={
            "resourceTypes": [], "requiredTags": [], "ratings": [modes.GENERAL],
            "newWorksSince": None, "maxEntriesPerUser": 1, "allowNewcomers": True,
        },
        divisions=[],
        awards=[],
        organizer_ids=[],
        results_draft=[],
    )
    await apply_admin_input(session, viewer, e, body, True)
    session.add(e)
    await session.flush()
    await add_log(session, e, viewer, "create", "建立活动（草稿）")
    await session.flush()
    return await event_dto(session, ctx, e)


async def admin_update(session, ctx: SiteContext, raw_id, body: dict) -> dict:
    viewer = require_staff(ctx)
    e = await visible_event(session, ctx, raw_id, for_update=True)
    before = (e.submit_start_at, e.submit_end_at, dict(e.eligibility or {}))
    await apply_admin_input(session, viewer, e, body, False)
    after = (e.submit_start_at, e.submit_end_at, dict(e.eligibility or {}))
    detail = (
        "修改了时间或资格（已提醒参赛者）"
        if before != after and event_state(e) != "draft"
        else "修改活动设置"
    )
    await add_log(session, e, viewer, "admin_edit", detail)
    await session.flush()
    return await event_dto(session, ctx, e)


async def admin_publish(session, ctx: SiteContext, raw_id) -> dict:
    viewer = require_staff(ctx)
    e = await visible_event(session, ctx, raw_id, for_update=True)
    if e.manual_state != "draft":
        raise conflict("活动已经发布过了")
    e.manual_state = "published"
    await add_log(session, e, viewer, "publish", "发布活动")
    await session.flush()
    return await event_dto(session, ctx, e)


async def admin_cancel(session, ctx: SiteContext, raw_id, body: dict) -> dict:
    viewer = require_staff(ctx)
    e = await visible_event(session, ctx, raw_id, for_update=True)
    e.manual_state = "cancelled"
    reason = clean_text((body or {}).get("reason"), 200).strip()
    await add_log(session, e, viewer, "cancel", f"取消活动{'：' + reason if reason else ''}")
    await session.flush()
    return await event_dto(session, ctx, e)


async def admin_organizers(session, ctx: SiteContext, raw_id, body: dict) -> dict:
    viewer = require_staff(ctx)
    e = await visible_event(session, ctx, raw_id, for_update=True)
    raw = (body or {}).get("userIds")
    if not isinstance(raw, list):
        raise invalid({"userIds": "缺少 userIds"})
    ids = list(dict.fromkeys(str(x) for x in raw))
    if len(ids) > MAX_ORGANIZERS:
        raise invalid({"userIds": f"最多指定 {MAX_ORGANIZERS} 个主办人"})
    found = set(
        (
            await session.execute(
                select(User.id).where(User.id.in_(ids or [""]), User.merged_into_id.is_(None))
            )
        ).scalars()
    )
    if any(i not in found for i in ids):
        raise invalid({"userIds": "有用户不存在"})
    if any(r != modes.GENERAL for r in eligibility_dto(e)["ratings"]) and ids:
        from models.site_account import SiteRestrictedAccess

        granted = set(
            (
                await session.execute(
                    select(SiteRestrictedAccess.user_id).where(
                        SiteRestrictedAccess.user_id.in_(ids), SiteRestrictedAccess.state == "granted"
                    )
                )
            ).scalars()
        )
        if any(i not in granted for i in ids):
            raise invalid({"userIds": "这个活动收 R18 作品，主办人必须有 R18 资格"})
    e.organizer_ids = ids
    users = await load_users(session, ids)
    names = "、".join(users[i]["displayName"] for i in ids if i in users) or "无"
    await add_log(session, e, viewer, "organizers", f"指定主办人：{names}")
    await session.flush()
    # 协议 0.5.0（EVT-04）：返回整个 Event；前端只读 .organizers，Event 里也有
    return await event_dto(session, ctx, e)
