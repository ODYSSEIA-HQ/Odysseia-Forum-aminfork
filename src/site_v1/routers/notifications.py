"""提醒（协议 3.7）、站内公告和规则中心（3.9）、用户处罚记录（3.10）。"""

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from sqlalchemy import String, and_, cast, func, or_, select

from models.site_me import SiteAnnouncement, SiteAnnouncementAck, SiteRuleDoc
from models.site_moderation import ModerationQueueItem, UserModerationRecord
from models.user import User
from models.work import Work
from shared.time_utils import utc_now
from site_v1 import mode as modes
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import iso_utc, load_users
from site_v1.errors import forbidden, invalid, not_found
from site_v1.paging import PageRequest, page_response, pagination
from site_v1.services import notifications as notes
from site_v1.services.moderation_common import site_muted_until
from site_v1.services.works import unknown_user

router = APIRouter(tags=["提醒和公告"])


# ---------------- 提醒 ----------------


@router.get("/notifications", summary="提醒（新的在前）")
async def list_notifications(
    viewer: CurrentUser,
    ctx: Ctx,
    category: str | None = Query(None),
    unread: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    categories = notes.parse_categories(category)
    if categories is None:
        raise invalid({"category": "分类不对"})
    async with get_session_factory()() as session:
        return await notes.list_notifications(
            session, ctx, page, categories, unread in ("1", "true")
        )


@router.get("/notifications/unread-counts", summary="各分类未读数")
async def unread_counts(viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        return await notes.unread_counts(session, ctx)


@router.post("/notifications/read", summary="标记已读；返回最新未读数")
async def read_notifications(
    viewer: CurrentUser, ctx: Ctx, body: dict | None = Body(None)
):
    if body is not None and not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    async with get_session_factory()() as session:
        await notes.mark_read(session, ctx, body or {})
        await session.commit()
        return await notes.unread_counts(session, ctx)


# ---------------- 站内公告 ----------------


def _audience_ok(row: SiteAnnouncement, ctx) -> bool:
    return row.audience == "all" or ctx.effective_mode == modes.R18


def _visible_announcements(ctx):
    q = select(SiteAnnouncement).where(
        SiteAnnouncement.deleted_at.is_(None),
        SiteAnnouncement.published_at <= utc_now(),
    )
    if ctx.effective_mode != modes.R18:
        q = q.where(SiteAnnouncement.audience == "all")
    return q


def announcement_dto(row: SiteAnnouncement) -> dict:
    return {
        "id": str(row.id),
        "audience": row.audience,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "requiresAck": row.requires_ack,
        "version": row.version,
        "publishedAt": iso_utc(row.published_at),
        "ruleSlug": row.rule_slug,
        "pinned": row.pinned,
    }


@router.get("/announcements", summary="公告板（置顶在前，新的在前）")
async def list_announcements(
    ctx: Ctx, kind: str | None = Query(None), page: PageRequest = Depends(pagination)
):
    q = _visible_announcements(ctx)
    if kind:
        if kind not in ("normal", "banner", "rule"):
            raise invalid({"kind": "只能是 normal、banner、rule"})
        q = q.where(SiteAnnouncement.kind == kind)
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    q.order_by(
                        SiteAnnouncement.pinned.desc(),
                        SiteAnnouncement.published_at.desc(),
                        SiteAnnouncement.id.desc(),
                    )
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
    return page_response(rows, page, announcement_dto)


@router.get(
    "/announcements/pending-ack", summary="还没确认的规则类公告（按版本号比对）"
)
async def pending_ack(viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        acked = (
            select(SiteAnnouncementAck.announcement_id)
            .where(
                SiteAnnouncementAck.user_id == viewer.user_id,
                SiteAnnouncementAck.announcement_id == SiteAnnouncement.id,
                SiteAnnouncementAck.version >= SiteAnnouncement.version,
            )
            .correlate(SiteAnnouncement)
            .exists()
        )
        rows = (
            (
                await session.execute(
                    _visible_announcements(ctx)
                    .where(SiteAnnouncement.requires_ack.is_(True), ~acked)
                    .order_by(SiteAnnouncement.published_at, SiteAnnouncement.id)
                )
            )
            .scalars()
            .all()
        )
    return [announcement_dto(r) for r in rows]


@router.post("/announcements/{id}/ack", summary="确认公告当前版本", status_code=204)
async def ack_announcement(id: str, viewer: CurrentUser, ctx: Ctx):
    from sqlalchemy.dialects.postgresql import insert

    async with get_session_factory()() as session:
        row = await session.get(SiteAnnouncement, int(id)) if id.isdigit() else None
        if row is None or row.deleted_at is not None or not _audience_ok(row, ctx):
            raise not_found()
        stmt = insert(SiteAnnouncementAck).values(
            user_id=viewer.user_id,
            announcement_id=row.id,
            version=row.version,
            acked_at=utc_now(),
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["user_id", "announcement_id"],
                set_={
                    "version": stmt.excluded.version,
                    "acked_at": stmt.excluded.acked_at,
                },
            )
        )
        await session.commit()
    return Response(status_code=204)


# ---------------- 规则中心 ----------------


def rule_dto(doc: SiteRuleDoc, with_body: bool) -> dict:
    result = {
        "slug": doc.slug,
        "title": doc.title,
        "summary": doc.summary,
        "version": doc.version,
        "updatedAt": iso_utc(doc.updated_at),
    }
    if with_body:
        result["body"] = doc.body
    return result


@router.get("/rules", summary="规则中心目录")
async def list_rules():
    async with get_session_factory()() as session:
        docs = (
            (
                await session.execute(
                    select(SiteRuleDoc).order_by(SiteRuleDoc.sort, SiteRuleDoc.slug)
                )
            )
            .scalars()
            .all()
        )
    return {"items": [rule_dto(d, False) for d in docs]}


@router.get("/rules/{slug}", summary="规则文档")
async def get_rule(slug: str):
    async with get_session_factory()() as session:
        doc = await session.get(SiteRuleDoc, slug)
        if doc is None:
            raise not_found()
        return rule_dto(doc, True)


# ---------------- 用户处罚记录 ----------------

MODERATION_PERMISSIONS = (
    "moderation.images",
    "moderation.content",
    "moderation.reports",
)


@router.get("/moderation/users/{id}/record", summary="用户的处罚和调整记录（审核员）")
async def user_record(id: str, viewer: CurrentUser):
    from site_v1.services.moderation import STRIKE_WINDOW

    if not any(viewer.has_permission(p) for p in MODERATION_PERMISSIONS):
        raise forbidden("你没有查看处罚记录的权限")
    async with get_session_factory()() as session:
        user = (
            (
                await session.execute(
                    select(User).where(or_(User.id == id, User.username == id))
                )
            )
            .scalars()
            .first()
        )
        if user is None or user.merged_into_id:
            raise not_found("用户不存在")
        records = (
            (
                await session.execute(
                    select(UserModerationRecord)
                    .where(UserModerationRecord.user_id == user.id)
                    .order_by(UserModerationRecord.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        users = await load_users(session, [user.id, *(r.by_user_id for r in records)])
        now = utc_now()
        strikes = sum(
            1 for r in records if r.strike and r.created_at >= now - STRIKE_WINDOW
        )
        own_works = select(cast(Work.id, String)).where(Work.author_id == user.id)
        reports = (
            await session.execute(
                select(func.count()).where(
                    ModerationQueueItem.type == "report",
                    or_(
                        and_(
                            ModerationQueueItem.target_type == "user",
                            ModerationQueueItem.target_id == user.id,
                        ),
                        and_(
                            ModerationQueueItem.target_type == "work",
                            ModerationQueueItem.target_id.in_(own_works),
                        ),
                    ),
                )
            )
        ).scalar_one()
        muted = await site_muted_until(session, user.id)

    def by(uid):
        return users.get(uid) or unknown_user(uid)

    return {
        "user": users.get(user.id) or unknown_user(user.id),
        "strikes90d": strikes,
        "mutedUntil": iso_utc(muted),
        "reportsReceived": int(reports),
        "penalties": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "until": iso_utc(r.until),
                "reason": r.reason,
                "by": by(r.by_user_id),
                "createdAt": iso_utc(r.created_at),
                "queueItemId": str(r.queue_item_id) if r.queue_item_id else None,
            }
            for r in records
            if r.kind in ("warning", "mute")
        ],
        "adjustments": [
            {
                "id": str(r.id),
                "workId": str(r.work_id) if r.work_id else None,
                "workTitle": r.work_title or "",
                "summary": r.summary or "",
                "reason": r.reason,
                "strike": r.strike,
                "by": by(r.by_user_id),
                "createdAt": iso_utc(r.created_at),
                "queueItemId": str(r.queue_item_id) if r.queue_item_id else None,
            }
            for r in records
            if r.kind == "adjustment"
        ],
    }
