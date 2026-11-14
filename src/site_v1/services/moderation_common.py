"""聊天外层、举报、审核队列共用的小工具：作品可见性、全站禁言、作品内禁言、
审核员看到的附件、作品摘要（ModerationWorkSummary）、用户占位。

作品可见性统一用 services/work_access.work_visible（和作品详情同一份判断）。
看不到一律按不存在处理。
"""

import re
from datetime import datetime

from sqlalchemy import or_, select

from models.attachment import Attachment
from models.site_moderation import UserModerationRecord
from models.site_tag import SiteTag, SiteWorkTag
from models.thread import Thread
from models.work import Work
from models.work_user_mute import WorkUserMute
from shared.time_utils import utc_now
from site_v1 import mode as modes
from site_v1.dto import iso_utc, user_dto
from site_v1.services.work_access import work_rating, work_visible

_INT_ID = re.compile(r"[1-9][0-9]{0,17}")


def parse_int_id(raw) -> int | None:
    """协议 ID 是字符串；本地自增 ID 只认纯数字，其他一律当不存在。"""
    raw = str(raw) if raw is not None else ""
    return int(raw) if _INT_ID.fullmatch(raw) else None


def rating_of(work: Work) -> str:
    return work_rating(work)


async def visible_work(session, ctx, raw_id) -> Work | None:
    """当前查看者能看到的作品（统一规则见 services/work_access）；看不到返回 None。"""
    work_id = parse_int_id(raw_id)
    if work_id is None:
        return None
    work = await session.get(Work, work_id)
    return work if await work_visible(session, ctx, work) else None


async def native_thread_id(session, work: Work) -> str | None:
    """作品的 Fluxer 原生主帖 ID；还没建好（sync_status=pending）返回 None。

    Fluxer 原生作品看 work.source_thread_id；Discord 来源作品看 thread 表里
    platform=fluxer、role=main 的投影（有的话）。
    """
    if work.source_platform == "fluxer":
        return work.source_thread_id or None
    row = (
        await session.execute(
            select(Thread.thread_id)
            .where(
                Thread.work_id == work.id,
                Thread.platform == "fluxer",
                Thread.role == "main",
                Thread.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(row) if row else None


async def works_by_native_thread(session, thread_ids: list[str]) -> dict[str, Work]:
    """原生主帖 ID → 作品（未读汇总用）。"""
    ids = [t for t in {str(t) for t in thread_ids if t} if t]
    if not ids:
        return {}
    result: dict[str, Work] = {}
    for work in (
        await session.execute(
            select(Work).where(
                Work.source_platform == "fluxer", Work.source_thread_id.in_(ids)
            )
        )
    ).scalars():
        result[work.source_thread_id] = work
    numeric = [int(t) for t in ids if t.isdigit() and t not in result]
    if numeric:
        rows = (
            await session.execute(
                select(Thread.thread_id, Work)
                .join(Work, Work.id == Thread.work_id)
                .where(
                    Thread.thread_id.in_(numeric),
                    Thread.platform == "fluxer",
                    Thread.role == "main",
                    Thread.deleted_at.is_(None),
                )
            )
        ).all()
        for thread_id, work in rows:
            result.setdefault(str(thread_id), work)
    return result


# ---------------- 禁言 ----------------


async def site_muted_until(session, user_id: str) -> datetime | None:
    """全站禁言（user_moderation_record.kind=mute）的截止时间；没有禁言返回 None。

    身份头模式下本人的全站禁言以网关 Principal.mutedUntil 为准（和本地记录取较晚的）。
    """
    from site_v1 import principal as principals

    now = utc_now()
    p = principals.for_user(user_id)
    gateway_until = p.muted_until if p is not None and p.muted_until and p.muted_until > now else None
    rows = (
        await session.execute(
            select(UserModerationRecord.until).where(
                UserModerationRecord.user_id == user_id,
                UserModerationRecord.kind == "mute",
                UserModerationRecord.until > now,
            )
        )
    ).scalars()
    local = max(rows, default=None)
    if gateway_until is None:
        return local
    return gateway_until if local is None or gateway_until > local else local


async def work_mute(session, work_id: int, user_id: str) -> tuple[bool, datetime | None]:
    """作品内禁言（V2 work_user_mute，和 WorkModerationService.effective_mute 同一规则）：
    返回 (是否禁言, 截止时间)；永久禁言截止时间为 None。"""
    now = utc_now()
    rows = (
        (
            await session.execute(
                select(WorkUserMute.expires_at).where(
                    WorkUserMute.work_id == work_id,
                    WorkUserMute.user_id == user_id,
                    WorkUserMute.revoked_at.is_(None),
                    or_(WorkUserMute.expires_at.is_(None), WorkUserMute.expires_at > now),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return False, None
    if any(r is None for r in rows):
        return True, None
    return True, max(rows)


# ---------------- 审核员看到的对象 ----------------


def attachment_url(att: Attachment) -> str | None:
    """审核员用的可访问地址：不按内容模式隐藏，未传完的不给。

    R18/R18G 和全年龄都走存储适配器的短期签名地址（审核员视角一律签发）。
    """
    if not att.storage_key or att.state == "uploading":
        return None
    from site_v1.storage import get_storage

    rating = att.rating or "general"
    return get_storage().read_url(
        att.storage_key,
        mime=att.mime,
        public=rating == "general",
        filename=att.filename,
        viewer_id=None,
    )


def reviewer_attachment(att: Attachment | None) -> dict | None:
    """协议 Attachment，按审核员视角：不按内容模式隐藏，一律给地址。"""
    if att is None:
        return None
    url = attachment_url(att)
    state = att.state if att.state in ("processing", "ready", "rejected") else "processing"
    data = {
        "id": str(att.id),
        "kind": att.kind if att.kind in ("image", "file") else "file",
        "mime": att.mime,
        "size": int(att.size or 0),
        "name": att.filename,
        "url": url,
        "placeholder": False,
        "thumbUrl": url if att.kind == "image" else None,
        "rating": att.rating if att.rating in modes.RATINGS else modes.GENERAL,
        "reviewState": att.review_state
        if att.review_state in ("auto", "pending_review", "reviewed")
        else "auto",
        "state": state,
        "rejectReason": att.reject_reason
        if att.reject_reason in ("rating_conflict", "blocked")
        else None,
    }
    if att.width is not None:
        data["width"] = int(att.width)
    if att.height is not None:
        data["height"] = int(att.height)
    return data


def missing_user(user_id: str) -> dict:
    """账号已经不在了（合并、删除）时的占位 User。"""
    return user_dto(
        user_id=user_id,
        username=f"user_{user_id}",
        display_name="已注销用户",
        avatar_url=None,
        created_at=None,
        discord_linked=False,
        is_newcomer=False,
    )


async def tag_refs(session, work_ids: list[int]) -> dict[int, list[dict]]:
    """作品上当前生效的协议 tag（TagRef）。"""
    ids = list({w for w in work_ids if w is not None})
    result: dict[int, list[dict]] = {w: [] for w in ids}
    if not ids:
        return result
    rows = (
        await session.execute(
            select(SiteWorkTag.work_id, SiteWorkTag.added_by, SiteTag)
            .join(SiteTag, SiteTag.id == SiteWorkTag.tag_id)
            .where(
                SiteWorkTag.work_id.in_(ids),
                SiteWorkTag.removed_at.is_(None),
                SiteTag.deleted_at.is_(None),
            )
            .order_by(SiteWorkTag.work_id, SiteTag.sort_order, SiteTag.id)
        )
    ).all()
    for work_id, added_by, tag in rows:
        result[work_id].append(
            {
                "id": str(tag.id),
                "name": tag.name,
                "restricts": bool(tag.restricts),
                "group": tag.group_id,
                "addedBy": added_by,
                "official": bool(tag.official),
            }
        )
    return result


def work_summary(
    work: Work | None,
    users: dict[str, dict],
    tags: dict[int, list[dict]],
    attachments: dict[int, Attachment],
) -> dict | None:
    """协议 ModerationWorkSummary（审核员视角，封面不按模式隐藏）。"""
    if work is None:
        return None
    author_id = work.author_id or ""
    moderation_state = work.moderation_state
    if moderation_state not in ("ok", "pending_review", "hidden", "removed"):
        moderation_state = "ok"
    return {
        "id": str(work.id),
        "title": work.title,
        "author": users.get(author_id) or missing_user(author_id),
        "rating": rating_of(work),
        "visibility": work.visibility or "public",
        "status": work.status,
        "moderationState": moderation_state,
        "tags": tags.get(work.id, []),
        "cover": reviewer_attachment(attachments.get(work.cover_attachment_id))
        if work.cover_attachment_id
        else None,
    }


def iso(value: datetime | None) -> str | None:
    return iso_utc(value)
