"""举报和审核队列（协议 3.10，10-25 第 11、12 条）。规则对齐假后端
contract-tests/mock-server/vendor/src/mock/handlers/moderation.ts 和 shared/lib/moderation.ts。

举报：
- 理由按对象类型限定；选 other 时 note 必填；note 最多 500 字；不能举报自己（用户）。
- 看不到的对象按不存在处理（404）：作品按有效模式，附件按分级和所属作品，tag 在全年龄
  模式下不能是限制可见的。collection / event / event_entry 是第二期对象，暂时一律 404。
  message / post / dc_archive_post 在 Fluxer（或 DC 存档）里，Forum 读不到原文，只记 ID。
- 同一对象的举报聚合到一个待处理的 report 队列项；同一账号对同一待处理对象再举报 409
  （report 表的部分唯一索引兜底）。队列项处理后，里面的举报一起变成 resolved，之后可以再举报。

审核队列：
- image 队列要 moderation.images 且本人 R18 资格 granted；report 队列要 moderation.reports
  （或 moderation.content）。没有权限 403。
- 图片项来自 attachment 表：review_state=pending_review 的图片在读队列时补成队列项
  （上传领域也可以直接调 enqueue_image，带上 source / appeal）。
- 处理：图片 approve / set_rating / block；举报 dismiss / adjust / remove / penalize，
  处罚可附在 adjust / remove 上。处罚、作品调整写 user_moderation_record。已处理 409。
"""

from datetime import timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from models.attachment import Attachment
from models.site_moderation import ModerationQueueItem, Report, UserModerationRecord
from models.site_tag import SiteTag, SiteWorkTag
from models.user import User
from models.work import Work
from shared.time_utils import utc_now
from site_v1 import mode as modes
from site_v1.dto import iso_utc, load_users
from site_v1.errors import SiteError, conflict, forbidden, invalid, not_found
from site_v1.services.work_access import work_visible
from site_v1.services.moderation_common import (
    missing_user,
    parse_int_id,
    rating_of,
    reviewer_attachment,
    tag_refs,
    work_summary,
)

# ---------------- 举报规则 ----------------

REASONS_BY_TARGET: dict[str, tuple[str, ...]] = {
    "work": ("rating_mismatch", "missing_warning", "missing_jailbreak", "illegal", "infringement", "other"),
    "upload": ("rating_mismatch", "illegal", "infringement", "other"),
    "message": ("rating_mismatch", "illegal", "spam", "harassment", "other"),
    "post": ("rating_mismatch", "illegal", "spam", "harassment", "infringement", "other"),
    "user": ("spam", "harassment", "illegal", "other"),
    "dc_archive_post": ("rating_mismatch", "illegal", "spam", "harassment", "infringement", "other"),
    "tag": ("illegal", "harassment", "spam", "other"),
    # 第二期对象：理由同假后端；对象本身暂时都不存在（404）
    "collection": ("illegal", "harassment", "spam", "infringement", "other"),
    "event": ("illegal", "spam", "harassment", "other"),
    "event_entry": ("rating_mismatch", "illegal", "infringement", "spam", "other"),
}
TARGET_LABEL = {
    "work": "作品",
    "message": "消息",
    "post": "帖子",
    "user": "用户",
    "upload": "图片",
    "dc_archive_post": "DC 存档楼层",
    "tag": "tag",
    "collection": "书单",
    "event": "活动",
    "event_entry": "参赛作品",
}
NOTE_MAX = 500
REASON_MAX = 500
TARGET_ID_MAX = 64
IMAGE_DECISIONS = ("approve", "set_rating", "block")
REPORT_DECISIONS = ("dismiss", "adjust", "remove", "penalize")
IMAGE_USAGES = ("cover", "gallery", "chat", "discussion")
MAX_REPORTS_SHOWN = 20
STRIKE_WINDOW = timedelta(days=90)


def validate_report(body: dict) -> dict[str, str]:
    fields: dict[str, str] = {}
    target_type = body.get("targetType")
    target_id = body.get("targetId")
    reason = body.get("reason")
    note = body.get("note")
    if not isinstance(target_type, str) or target_type not in REASONS_BY_TARGET:
        fields["targetType"] = "举报对象类型不对"
    if (
        not isinstance(target_id, str)
        or not target_id.strip()
        or len(target_id) > TARGET_ID_MAX
    ):
        fields["targetId"] = "缺少举报对象"
    if not isinstance(reason, str) or not reason:
        fields["reason"] = "请选择举报理由"
    elif target_type in REASONS_BY_TARGET and reason not in REASONS_BY_TARGET[target_type]:
        fields["reason"] = "这个理由不适用于该对象"
    if note is not None and not isinstance(note, str):
        fields["note"] = "补充说明应该是文字"
    else:
        text = (note or "").strip()
        if len(text) > NOTE_MAX:
            fields["note"] = f"补充说明最多 {NOTE_MAX} 字"
        elif reason == "other" and not text:
            fields["note"] = "选「其他」时请写一下具体情况"
    return fields


def _snapshot(title, excerpt=None, author_id=None, work_id=None, attachment_id=None):
    return {
        "title": title,
        "excerpt": excerpt,
        "authorId": author_id,
        "workId": work_id,
        "attachmentId": attachment_id,
    }


async def _attachment_visible(session, ctx, att: Attachment) -> bool:
    viewer = ctx.viewer
    if viewer is not None and att.owner_id == viewer.user_id:
        return True
    if att.state != "ready":
        return False
    rating = att.rating or modes.GENERAL
    if not ctx.can_view(rating, "public" if rating == modes.GENERAL else "restricted"):
        return False
    if att.work_id is not None:
        return await work_visible(session, ctx, await session.get(Work, att.work_id))
    return True


async def target_snapshot(session, ctx, target_type: str, target_id: str) -> dict:
    """举报时存的对象快照；对象不存在或查看者看不到时抛 404。"""
    if target_type == "work":
        work_id = parse_int_id(target_id)
        work = await session.get(Work, work_id) if work_id else None
        if not await work_visible(session, ctx, work):
            raise not_found()
        return _snapshot(
            work.title,
            (work.summary or work.description or "")[:120] or None,
            work.author_id,
            str(work.id),
        )
    if target_type == "user":
        user = await session.get(User, target_id)
        if user is None or user.merged_into_id:
            raise not_found()
        return _snapshot(user.display_name or user.username, None, user.id)
    if target_type == "upload":
        att_id = parse_int_id(target_id)
        att = await session.get(Attachment, att_id) if att_id else None
        if (
            att is None
            or att.deleted_at is not None
            or att.state == "uploading"
            or not await _attachment_visible(session, ctx, att)
        ):
            raise not_found()
        return _snapshot(
            att.filename,
            None,
            att.owner_id,
            str(att.work_id) if att.work_id else None,
            str(att.id),
        )
    if target_type == "tag":
        tag_id = parse_int_id(target_id)
        tag = await session.get(SiteTag, tag_id) if tag_id else None
        if tag is None or tag.deleted_at is not None:
            raise not_found()
        # 限制可见的 tag（R18、破限……）全年龄模式下看不到
        if (tag.restricts or (tag.rating or modes.GENERAL) != modes.GENERAL) and (
            ctx.effective_mode != modes.R18
            or (tag.rating or modes.GENERAL) not in ctx.visible_ratings()
        ):
            raise not_found()
        return _snapshot(
            tag.name,
            "官方 tag" if tag.official else "自由 tag",
            None if tag.official else tag.created_by,
        )
    if target_type in ("collection", "event", "event_entry"):
        # 书单、活动、参赛作品是第二期，本路线还没有这些对象
        raise not_found()
    # message / post / dc_archive_post：原文在 Fluxer（或 DC 存档），Forum 读不到，只记 ID
    return _snapshot(f"{TARGET_LABEL[target_type]} {target_id}")


async def create_report(session, ctx, body: dict) -> dict:
    viewer = ctx.require_user()
    fields = validate_report(body)
    if fields:
        raise invalid(fields)
    target_type: str = body["targetType"]
    target_id: str = body["targetId"].strip()
    if target_type == "user" and target_id == viewer.user_id:
        raise invalid({"targetId": "不能举报自己"})

    duplicate = SiteError(
        409, "conflict", "你已经举报过这个内容，审核员处理后会通知你"
    )
    if (
        await session.execute(
            select(Report.id).where(
                Report.reporter_id == viewer.user_id,
                Report.target_type == target_type,
                Report.target_id == target_id,
                Report.state == "pending",
            )
        )
    ).first():
        raise duplicate
    snapshot = await target_snapshot(session, ctx, target_type, target_id)

    item_id = await _pending_report_item(session, target_type, target_id, snapshot)
    report = Report(
        reporter_id=viewer.user_id,
        target_type=target_type,
        target_id=target_id,
        reason=body["reason"],
        note=(body.get("note") or "").strip(),
        queue_item_id=item_id,
    )
    try:
        async with session.begin_nested():
            session.add(report)
    except IntegrityError:
        raise duplicate
    receipt = {"id": str(report.id), "createdAt": iso_utc(report.created_at)}
    await session.commit()
    return receipt


async def _pending_report_item(session, target_type, target_id, snapshot) -> int:
    """找到或建这个对象的待处理举报队列项（并发时靠部分唯一索引去重）。"""
    stmt = (
        insert(ModerationQueueItem)
        .values(
            type="report",
            state="pending",
            source="report",
            target_type=target_type,
            target_id=target_id,
            subject_user_id=snapshot.get("authorId"),
            work_id=parse_int_id(snapshot.get("workId")),
            attachment_id=parse_int_id(snapshot.get("attachmentId")),
            target_snapshot=snapshot,
            appealed=False,
            created_at=utc_now(),
        )
        .on_conflict_do_nothing(
            index_elements=["type", "target_type", "target_id"],
            index_where=ModerationQueueItem.state == "pending",
        )
        .returning(ModerationQueueItem.id)
    )
    item_id = (await session.execute(stmt)).scalar_one_or_none()
    if item_id is not None:
        return item_id
    return (
        await session.execute(
            select(ModerationQueueItem.id).where(
                ModerationQueueItem.type == "report",
                ModerationQueueItem.state == "pending",
                ModerationQueueItem.target_type == target_type,
                ModerationQueueItem.target_id == target_id,
            )
        )
    ).scalar_one()


# ---------------- 权限 ----------------


def require_queue_access(viewer, queue_type: str) -> None:
    if queue_type == "image":
        if not viewer.has_permission("moderation.images"):
            raise forbidden("你没有图片审核权限")
        if viewer.access.state != "granted":
            raise forbidden(
                "图片审核员本人需要有 R18 资格", reason="restricted_access_required"
            )
    elif not (
        viewer.has_permission("moderation.reports")
        or viewer.has_permission("moderation.content")
    ):
        raise forbidden("你没有处理举报的权限")


# ---------------- 图片入队 ----------------


async def enqueue_image(
    session,
    attachment: Attachment,
    *,
    source: str = "model",
    appeal_note: str | None = None,
) -> int | None:
    """把一张图片放进图片审核队列（已有待处理项时不重复）。上传领域可直接调用。"""
    stmt = (
        insert(ModerationQueueItem)
        .values(
            type="image",
            state="pending",
            source=source,
            target_type="upload",
            target_id=str(attachment.id),
            attachment_id=attachment.id,
            work_id=attachment.work_id,
            subject_user_id=attachment.owner_id,
            auto_rating=attachment.auto_rating,
            model_scores=attachment.model_scores,
            usage=attachment.purpose if attachment.purpose in IMAGE_USAGES else None,
            appealed=appeal_note is not None or source == "appeal",
            appeal_note=appeal_note,
            created_at=utc_now(),
        )
        .on_conflict_do_nothing(
            index_elements=["type", "target_type", "target_id"],
            index_where=ModerationQueueItem.state == "pending",
        )
        .returning(ModerationQueueItem.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def sync_image_queue(session) -> None:
    """attachment 里待复核（review_state=pending_review）、还没进队列的图片补成队列项。"""
    pending_item = exists().where(
        ModerationQueueItem.type == "image",
        ModerationQueueItem.state == "pending",
        ModerationQueueItem.attachment_id == Attachment.id,
    )
    rows = (
        (
            await session.execute(
                select(Attachment)
                .where(
                    Attachment.kind == "image",
                    Attachment.review_state == "pending_review",
                    Attachment.deleted_at.is_(None),
                    Attachment.state.in_(("processing", "ready", "rejected")),
                    Attachment.purpose.in_(IMAGE_USAGES),
                    ~pending_item,
                )
                .order_by(Attachment.id)
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    for att in rows:
        await enqueue_image(session, att)
    if rows:
        await session.commit()


# ---------------- 列表和序列化 ----------------


async def list_queue(session, viewer, queue_type: str, state: str, page) -> dict:
    require_queue_access(viewer, queue_type)
    if queue_type == "image" and state == "pending":
        await sync_image_queue(session)
    q = select(ModerationQueueItem).where(
        ModerationQueueItem.type == queue_type, ModerationQueueItem.state == state
    )
    if queue_type == "image":
        # 附件被删了的图片项不再展示
        q = q.where(
            exists().where(
                Attachment.id == ModerationQueueItem.attachment_id,
                Attachment.deleted_at.is_(None),
            )
        )
    if state == "resolved":
        q = q.order_by(
            ModerationQueueItem.resolved_at.desc().nulls_last(),
            ModerationQueueItem.id.desc(),
        )
    elif queue_type == "report":
        # 举报人多的排前面，一样多时先来的先处理
        reporters = (
            select(func.count(func.distinct(Report.reporter_id)))
            .where(Report.queue_item_id == ModerationQueueItem.id)
            .scalar_subquery()
        )
        q = q.order_by(
            reporters.desc(), ModerationQueueItem.created_at, ModerationQueueItem.id
        )
    else:
        q = q.order_by(ModerationQueueItem.created_at, ModerationQueueItem.id)
    rows = (
        (await session.execute(q.offset(page.offset).limit(page.limit + 1)))
        .scalars()
        .all()
    )
    has_more = len(rows) > page.limit
    rows = rows[: page.limit]
    items = await serialize_items(session, rows)
    from site_v1.paging import encode_cursor

    return {
        "items": items,
        "nextCursor": encode_cursor({"o": page.offset + len(rows)})
        if has_more and rows
        else None,
    }


async def _strikes(session, user_ids: list[str]) -> dict[str, int]:
    ids = list({u for u in user_ids if u})
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(UserModerationRecord.user_id, func.count())
            .where(
                UserModerationRecord.user_id.in_(ids),
                UserModerationRecord.strike.is_(True),
                UserModerationRecord.created_at >= utc_now() - STRIKE_WINDOW,
            )
            .group_by(UserModerationRecord.user_id)
        )
    ).all()
    return {u: int(n) for u, n in rows}


async def serialize_items(session, items: list[ModerationQueueItem]) -> list[dict]:
    """批量序列化队列项（ImageQueueItem / ReportQueueItem）。"""
    if not items:
        return []
    item_ids = [i.id for i in items]
    reports: dict[int, list[Report]] = {i: [] for i in item_ids}
    for r in (
        await session.execute(
            select(Report)
            .where(Report.queue_item_id.in_(item_ids))
            .order_by(Report.created_at.desc(), Report.id.desc())
        )
    ).scalars():
        reports[r.queue_item_id].append(r)

    # 作品、附件
    work_ids: set[int] = set()
    att_ids: set[int] = set()
    for item in items:
        snap = item.target_snapshot or {}
        if item.work_id:
            work_ids.add(item.work_id)
        if item.type == "report" and item.target_type == "work":
            wid = parse_int_id(item.target_id)
            if wid:
                work_ids.add(wid)
        if item.attachment_id:
            att_ids.add(item.attachment_id)
        if parse_int_id(snap.get("attachmentId")):
            att_ids.add(parse_int_id(snap.get("attachmentId")))
    works = {
        w.id: w
        for w in (
            await session.execute(select(Work).where(Work.id.in_(work_ids)))
        ).scalars()
    } if work_ids else {}
    att_ids |= {w.cover_attachment_id for w in works.values() if w.cover_attachment_id}
    attachments = {
        a.id: a
        for a in (
            await session.execute(select(Attachment).where(Attachment.id.in_(att_ids)))
        ).scalars()
    } if att_ids else {}
    tags = await tag_refs(session, list(works))

    # 作者、上传者、举报人、处理人
    def author_of(item) -> str | None:
        snap = item.target_snapshot or {}
        if item.type == "image":
            att = attachments.get(item.attachment_id)
            return att.owner_id if att else item.subject_user_id
        if item.target_type == "user":
            return item.target_id
        if item.target_type == "work":
            w = works.get(parse_int_id(item.target_id))
            if w and w.author_id:
                return w.author_id
        return snap.get("authorId") or item.subject_user_id

    user_ids: set[str] = set()
    for item in items:
        if author_of(item):
            user_ids.add(author_of(item))
        if item.resolution and item.resolution.get("by"):
            user_ids.add(item.resolution["by"])
        for r in reports[item.id][:MAX_REPORTS_SHOWN]:
            user_ids.add(r.reporter_id)
    user_ids |= {w.author_id for w in works.values() if w.author_id}
    users = await load_users(session, user_ids)
    strikes = await _strikes(
        session, [author_of(i) for i in items if i.type == "report"]
    )

    def user(uid: str) -> dict:
        return users.get(uid) or missing_user(uid)

    def resolution(item) -> dict | None:
        r = item.resolution
        if not r:
            return None
        return {
            "decision": r.get("decision"),
            "rating": r.get("rating"),
            "addTags": list(r.get("addTags") or []),
            "removeTags": list(r.get("removeTags") or []),
            "penalty": r.get("penalty"),
            "reason": r.get("reason") or "",
            "by": user(r.get("by") or ""),
            "at": r.get("at") or iso_utc(item.resolved_at),
        }

    def summary(work_id) -> dict | None:
        return work_summary(works.get(work_id), users, tags, attachments)

    result = []
    for item in items:
        base = {
            "id": str(item.id),
            "type": item.type,
            "state": item.state,
            "source": item.source,
            "createdAt": iso_utc(item.created_at),
            "resolution": resolution(item),
        }
        if item.type == "image":
            att = attachments.get(item.attachment_id)
            scores = item.model_scores or (att.model_scores if att else None) or {}
            auto = item.auto_rating or (att.auto_rating if att else None)
            usage = item.usage or (att.purpose if att else None)
            uploader = att.owner_id if att else (item.subject_user_id or "")
            result.append(
                {
                    **base,
                    "attachment": reviewer_attachment(att),
                    "autoRating": auto
                    if auto in ("general", "r18", "r18g", "suspect")
                    else "suspect",
                    "modelScores": {
                        k: float(scores.get(k) or 0) for k in ("general", "r18", "r18g")
                    },
                    "usage": usage if usage in IMAGE_USAGES else "gallery",
                    "uploader": user(uploader),
                    "work": summary(item.work_id or (att.work_id if att else None)),
                    "appealed": bool(item.appealed),
                    "appealNote": item.appeal_note,
                }
            )
            continue
        snap = item.target_snapshot or {}
        live_work = (
            works.get(parse_int_id(item.target_id))
            if item.target_type == "work"
            else works.get(parse_int_id(snap.get("workId")))
        )
        author_id = author_of(item)
        att_id = (
            parse_int_id(item.target_id)
            if item.target_type == "upload"
            else parse_int_id(snap.get("attachmentId"))
        )
        title = snap.get("title") or item.target_id
        excerpt = snap.get("excerpt")
        if item.target_type == "work" and live_work is not None:
            title = live_work.title
            excerpt = (live_work.summary or live_work.description or "")[:120] or None
        item_reports = reports[item.id]
        counts: dict[str, int] = {}
        for r in item_reports:
            counts[r.reason] = counts.get(r.reason, 0) + 1
        result.append(
            {
                **base,
                "target": {
                    "type": item.target_type,
                    "id": item.target_id,
                    "title": title,
                    "excerpt": excerpt,
                    "author": user(author_id) if author_id else None,
                    "work": work_summary(live_work, users, tags, attachments),
                    "attachment": reviewer_attachment(attachments.get(att_id))
                    if att_id
                    else None,
                },
                "reportCount": len({r.reporter_id for r in item_reports}),
                "reasons": [
                    {"reason": reason, "count": n}
                    for reason, n in sorted(counts.items(), key=lambda kv: -kv[1])
                ],
                "reports": [
                    {
                        "id": str(r.id),
                        "reporter": user(r.reporter_id),
                        "reason": r.reason,
                        "note": r.note or "",
                        "createdAt": iso_utc(r.created_at),
                    }
                    for r in item_reports[:MAX_REPORTS_SHOWN]
                ],
                "authorStrikes90d": strikes.get(author_id, 0) if author_id else 0,
            }
        )
    return result


# ---------------- 处理 ----------------


def validate_resolve(queue_type: str, body: dict, target_type: str | None) -> dict:
    """校验处理请求（规则同假后端 validateResolve）。返回规整后的请求。"""
    fields: dict[str, str] = {}
    decision = body.get("decision")
    allowed = IMAGE_DECISIONS if queue_type == "image" else REPORT_DECISIONS
    if decision not in allowed:
        raise invalid({"decision": "处理方式不对"})
    rating = body.get("rating")
    if rating is not None and rating not in modes.RATINGS:
        fields["rating"] = "分级不对"
    if decision == "set_rating" and not rating:
        fields["rating"] = "请选择分级"
    tags: dict[str, list[str]] = {}
    for key in ("addTags", "removeTags"):
        value = body.get(key)
        if value is None:
            tags[key] = []
        elif not isinstance(value, list) or not all(
            isinstance(v, str) and v for v in value
        ):
            fields[key] = "应该是 tag ID 列表"
            tags[key] = []
        else:
            tags[key] = list(dict.fromkeys(value))
    if decision == "adjust":
        if target_type != "work":
            fields["decision"] = "只有作品可以调整分级和 tag"
        elif not rating and not tags["addTags"] and not tags["removeTags"]:
            fields["rating"] = "请改分级或增删 tag"
    if decision == "remove" and target_type == "user":
        fields["decision"] = "用户不能下架，请用处罚"
    penalty = body.get("penalty")
    if decision == "penalize" and not penalty:
        fields["penalty"] = "请选择处罚"
    if penalty is not None:
        if queue_type == "image":
            fields["penalty"] = "图片审核不在这里处罚"
        elif decision == "dismiss":
            fields["penalty"] = "驳回时不能处罚"
        elif not isinstance(penalty, dict) or penalty.get("kind") not in ("warning", "mute"):
            fields["penalty"] = "处罚类型不对"
        elif penalty["kind"] == "mute" and not (
            isinstance(penalty.get("days"), int)
            and not isinstance(penalty.get("days"), bool)
            and 0 < penalty["days"] <= 3650
        ):
            fields["penalty"] = "请选择禁言天数"
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        fields["reason"] = "理由应该是文字"
        reason = ""
    reason = (reason or "").strip()
    if decision not in ("approve", "dismiss") and not reason:
        fields["reason"] = "请写处理理由（作者会看到）"
    if len(reason) > REASON_MAX:
        fields["reason"] = f"理由最多 {REASON_MAX} 字"
    if fields:
        raise invalid(fields)
    clean_penalty = None
    if penalty:
        clean_penalty = {"kind": penalty["kind"]}
        if penalty["kind"] == "mute":
            clean_penalty["days"] = penalty["days"]
    return {
        "decision": decision,
        "rating": rating,
        "addTags": tags["addTags"],
        "removeTags": tags["removeTags"],
        "penalty": clean_penalty,
        "reason": reason,
    }


RATING_NAME = {"general": "全年龄", "r18": "R18", "r18g": "R18G"}


async def _active_work_tags(session, work_id: int) -> list[tuple[SiteWorkTag, SiteTag]]:
    return (
        await session.execute(
            select(SiteWorkTag, SiteTag)
            .join(SiteTag, SiteTag.id == SiteWorkTag.tag_id)
            .where(SiteWorkTag.work_id == work_id, SiteWorkTag.removed_at.is_(None))
        )
    ).all()


async def _recompute_visibility(session, work: Work) -> None:
    restricting = any(
        tag.restricts and tag.deleted_at is None
        for _, tag in await _active_work_tags(session, work.id)
    )
    work.visibility = modes.work_visibility(rating_of(work), restricting)


async def set_work_rating(session, work: Work, rating: str, viewer) -> None:
    """站务改作品分级：分级字段走作品领域的统一入口 works.set_classification
    （有 Fluxer 帖子时排 V2 的分类移动任务），再换分级组 tag、重算 visibility。"""
    from site_v1.services.works import set_classification

    by = viewer.user_id
    await set_classification(
        session, work, rating, actor=viewer, mode=modes.R18, is_admin=True
    )
    now = utc_now()
    rating_tag = (
        await session.execute(
            select(SiteTag).where(
                SiteTag.group_id == "rating",
                SiteTag.rating == rating,
                SiteTag.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if rating_tag is not None:
        has_it = False
        for binding, tag in await _active_work_tags(session, work.id):
            if tag.group_id != "rating":
                continue
            if tag.id == rating_tag.id:
                has_it = True
            else:
                binding.removed_at = now
                binding.removed_by = by
        if not has_it:
            session.add(SiteWorkTag(work_id=work.id, tag_id=rating_tag.id, added_by=by))
        await session.flush()
    await _recompute_visibility(session, work)
    work.updated_at = now


async def _adjust_tags(session, work: Work, add: list[str], remove: list[str], by: str):
    """站务增删 tag（分级组、资源类型组不在这里改）。返回 (加上的名字, 去掉的名字)。"""
    ids = {parse_int_id(t) for t in add + remove}
    known = {
        t.id: t
        for t in (
            await session.execute(
                select(SiteTag).where(
                    SiteTag.id.in_([i for i in ids if i]), SiteTag.deleted_at.is_(None)
                )
            )
        ).scalars()
    } if any(ids) else {}
    bad = [
        t
        for t in add + remove
        if parse_int_id(t) not in known or known[parse_int_id(t)].group_id in ("rating", "type")
    ]
    if bad:
        raise invalid({"addTags": f"不认识或不能在这里改的 tag：{'、'.join(bad)}"})
    active = {b.tag_id: b for b, _ in await _active_work_tags(session, work.id)}
    now = utc_now()
    added, removed = [], []
    for t in add:
        tag = known[parse_int_id(t)]
        if tag.id not in active:
            session.add(SiteWorkTag(work_id=work.id, tag_id=tag.id, added_by=by))
            added.append(tag.name)
    for t in remove:
        tag = known[parse_int_id(t)]
        binding = active.get(tag.id)
        if binding is not None and tag.id not in {parse_int_id(a) for a in add}:
            binding.removed_at = now
            binding.removed_by = by
            removed.append(tag.name)
    await session.flush()
    await _recompute_visibility(session, work)
    return added, removed


def _adjustment(
    item, user_id, work: Work | None, summary, reason, strike, by, work_title=None
):
    return UserModerationRecord(
        user_id=user_id,
        kind="adjustment",
        reason=reason,
        strike=strike,
        work_id=work.id if work else None,
        work_title=work.title if work else (work_title or ""),
        summary=summary,
        by_user_id=by,
        queue_item_id=item.id,
    )


async def _clear_pending_review(session, work: Work, except_item: int) -> None:
    """作品没有别的待审图片了：撤掉 pending_review。"""
    other = (
        await session.execute(
            select(ModerationQueueItem.id).where(
                ModerationQueueItem.type == "image",
                ModerationQueueItem.state == "pending",
                ModerationQueueItem.work_id == work.id,
                ModerationQueueItem.id != except_item,
            ).limit(1)
        )
    ).first()
    if other is None and work.moderation_state == "pending_review":
        work.moderation_state = "ok"


async def _resolve_image(session, item, req, viewer) -> None:
    att = await session.get(Attachment, item.attachment_id) if item.attachment_id else None
    if att is None or att.deleted_at is not None:
        raise not_found("这张图片已经不在了")
    work_id = item.work_id or att.work_id
    work = await session.get(Work, work_id) if work_id else None
    by = viewer.user_id
    if req["decision"] == "block":
        att.state = "rejected"
        att.reject_reason = "blocked"
        att.review_state = "reviewed"
        if work is not None:
            work.gallery_attachment_ids = [
                i for i in (work.gallery_attachment_ids or []) if i != att.id
            ]
            if work.cover_attachment_id == att.id:
                work.cover_attachment_id = None
            work.updated_at = utc_now()
            if work.author_id:
                session.add(
                    _adjustment(item, work.author_id, work, "拦截图片", req["reason"], True, by)
                )
    else:
        work_rating = rating_of(work) if work else modes.GENERAL
        rating = req["rating"] if req["decision"] == "set_rating" else (req["rating"] or work_rating)
        att.rating = rating
        att.review_state = "reviewed"
        att.state = "ready"
        att.reject_reason = None
        # 图片比作品分级高：作品随之上调（01 号文档 5.1）
        if work is not None and modes.rating_rank(rating) > modes.rating_rank(work_rating):
            await set_work_rating(session, work, rating, viewer)
            if work.author_id:
                session.add(
                    _adjustment(
                        item,
                        work.author_id,
                        work,
                        f"分级 {RATING_NAME[work_rating]} → {RATING_NAME[rating]}（图片复核）",
                        req["reason"],
                        False,
                        by,
                    )
                )
    if work is not None:
        await _clear_pending_review(session, work, item.id)


async def _remove_work(session, work: Work, viewer) -> None:
    """下架作品：协议 status=removed；同时复用 V2 的软删除（V2 列表也不再出现，原生帖投影策略）。"""
    from work.work_moderation_service import WorkModerationService

    work.status = "removed"
    work.moderation_state = "removed"
    work.updated_at = utc_now()
    if work.deleted_at is not None:
        return
    principal = {
        **viewer.to_v2_principal(modes.GENERAL),
        "is_admin": True,
    }
    service = WorkModerationService(session)
    if viewer.discord_user_id and str(viewer.discord_user_id).isdigit():
        await service.soft_delete(work, principal, True)
        return
    # 站务没有 Discord 绑定：OperationLog.actor_id 只能留空，其余和 soft_delete 一致
    from models.operation_log import OperationLog

    work.deleted_at = utc_now()
    work.deletion_reason = "moderation"
    await service.policy_tasks(work.id, principal, "forum_soft_delete", True)
    session.add(
        OperationLog(
            type="work.delete",
            target_type="work",
            target_id=work.id,
            actor_id=None,
            fluxer_actor_id=viewer.user_id,
            detail={"reason": "moderation"},
        )
    )
    await session.flush()


async def _resolve_report(session, item, req, viewer) -> None:
    by = viewer.user_id
    snap = item.target_snapshot or {}
    work = None
    if item.target_type == "work":
        wid = parse_int_id(item.target_id)
        work = await session.get(Work, wid) if wid else None
    author_id = None
    if item.target_type == "user":
        author_id = item.target_id
    elif work is not None and work.author_id:
        author_id = work.author_id
    else:
        author_id = snap.get("authorId") or item.subject_user_id

    if req["decision"] == "adjust":
        if work is None:
            raise not_found("作品已经不在了")
        parts = []
        before = rating_of(work)
        if req["rating"] and req["rating"] != before:
            await set_work_rating(session, work, req["rating"], viewer)
            work.rating_lock = {"by": by, "at": iso_utc(utc_now()), "reason": req["reason"]}
            parts.append(f"分级 {RATING_NAME[before]} → {RATING_NAME[req['rating']]}")
        added, removed = await _adjust_tags(
            session, work, req["addTags"], req["removeTags"], by
        )
        if added:
            parts.append("加 tag " + "、".join(added))
        if removed:
            parts.append("去掉 tag " + "、".join(removed))
        work.updated_at = utc_now()
        if work.author_id:
            session.add(
                _adjustment(
                    item, work.author_id, work, "；".join(parts) or "无变化", req["reason"], True, by
                )
            )
    elif req["decision"] == "remove":
        summary = None
        if item.target_type == "work" and work is not None:
            await _remove_work(session, work, viewer)
            summary = "作品下架"
        elif item.target_type == "tag":
            tag_id = parse_int_id(item.target_id)
            tag = await session.get(SiteTag, tag_id) if tag_id else None
            if tag is not None and tag.group_id in ("rating", "type"):
                raise invalid({"decision": "分级和资源类型 tag 不能禁用"})
            if tag is not None and tag.deleted_at is None:
                now = utc_now()
                tag.deleted_at = now
                await session.execute(
                    update(SiteWorkTag)
                    .where(SiteWorkTag.tag_id == tag.id, SiteWorkTag.removed_at.is_(None))
                    .values(removed_at=now, removed_by=by)
                )
            summary = "禁用 tag"
        elif item.target_type == "upload":
            att_id = parse_int_id(item.target_id)
            att = await session.get(Attachment, att_id) if att_id else None
            if att is not None:
                att.state = "rejected"
                att.reject_reason = "blocked"
                att.review_state = "reviewed"
                if att.work_id:
                    owner = await session.get(Work, att.work_id)
                    if owner is not None:
                        owner.gallery_attachment_ids = [
                            i for i in (owner.gallery_attachment_ids or []) if i != att.id
                        ]
                        if owner.cover_attachment_id == att.id:
                            owner.cover_attachment_id = None
                        work = owner
            summary = "删除图片"
        else:
            # 消息、帖子、DC 存档楼层的删除由聊天 / 讨论模块执行，这里只记录
            summary = f"删除{TARGET_LABEL.get(item.target_type, '内容')}"
        if author_id:
            session.add(
                _adjustment(
                    item, author_id, work, summary, req["reason"], True, by,
                    work_title=snap.get("title"),
                )
            )

    if req["penalty"]:
        if not author_id:
            raise invalid({"penalty": "找不到这个内容的作者，没法处罚"})
        penalty = req["penalty"]
        session.add(
            UserModerationRecord(
                user_id=author_id,
                kind=penalty["kind"],
                until=utc_now() + timedelta(days=penalty["days"])
                if penalty["kind"] == "mute"
                else None,
                reason=req["reason"],
                strike=False,
                by_user_id=by,
                queue_item_id=item.id,
            )
        )


async def resolve(session, viewer, raw_id: str, body: dict) -> dict:
    item_id = parse_int_id(raw_id)
    if item_id is None:
        raise not_found()
    item = (
        await session.execute(
            select(ModerationQueueItem)
            .where(ModerationQueueItem.id == item_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found()
    require_queue_access(viewer, item.type)
    if item.state != "pending":
        raise conflict("这一项已经被处理过了，请刷新")
    req = validate_resolve(item.type, body, item.target_type)

    if item.type == "image":
        await _resolve_image(session, item, req, viewer)
    else:
        await _resolve_report(session, item, req, viewer)

    now = utc_now()
    item.state = "resolved"
    item.resolved_by = viewer.user_id
    item.resolved_at = now
    item.resolution = {**req, "by": viewer.user_id, "at": iso_utc(now)}
    # 里面的举报一起结束，之后同一个人可以再举报（新的队列项）
    await session.execute(
        update(Report)
        .where(Report.queue_item_id == item.id, Report.state == "pending")
        .values(state="resolved")
    )
    await session.flush()
    data = (await serialize_items(session, [item]))[0]
    await session.commit()
    return data
