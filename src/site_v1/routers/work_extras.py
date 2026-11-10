"""作品内：公告、确认、规则、钉选、关注、DC 原帖存档（协议 3.2、3.19）。

作品内禁言（/works/{id}/mutes）在聊天块，不在这里。
"""

from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response
from sqlalchemy import func, select

from models.site_me import SiteDcArchivePost, SiteWorkPin, SiteWorkRules
from models.user import User
from models.work_announcement import WorkAnnouncement
from shared.time_utils import utc_now
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import external_image, iso_utc, load_users
from site_v1.errors import forbidden, invalid, not_found
from site_v1.paging import PageRequest, page_response, pagination
from site_v1.services import work_extras as wx
from site_v1.services.work_access import (
    can_manage_work,
    get_editable_work,
    get_visible_work,
)
from site_v1.services.works import unknown_user

router = APIRouter(prefix="/works", tags=["作品"])

ANNOUNCEMENT_TITLE_MAX = 60
TEXT_MAX = 5000
PIN_MAX = 50
PIN_EXCERPT_MAX = 500


def _text(value) -> str:
    """去掉控制字符（保留换行、制表符）。"""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch >= " " or ch in "\t\n\r")


def _obj(body) -> dict:
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    return body


# ---------------- 公告 ----------------


def _announcement_dto(row: WorkAnnouncement, seq: int, users: dict) -> dict:
    return {
        "id": str(row.id),
        "workId": str(row.work_id),
        "title": row.title or "",
        "body": row.body,
        "version": int(seq),
        "author": users.get(row.author_id) or unknown_user(row.author_id),
        "createdAt": iso_utc(row.created_at),
    }


@router.get("/{id}/announcements", summary="作品公告（新的在前）")
async def list_announcements(
    id: str, ctx: Ctx, page: PageRequest = Depends(pagination)
):
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        numbered = (
            select(WorkAnnouncement.id.label("aid"), wx.announcement_seq())
            .where(WorkAnnouncement.work_id == work.id)
            .subquery()
        )
        rows = (
            await session.execute(
                select(WorkAnnouncement, numbered.c.seq)
                .join(numbered, numbered.c.aid == WorkAnnouncement.id)
                .where(WorkAnnouncement.withdrawn_at.is_(None))
                .order_by(numbered.c.seq.desc())
                .offset(page.offset)
                .limit(page.limit + 1)
            )
        ).all()
        users = await load_users(session, [r[0].author_id for r in rows])
    return page_response(rows, page, lambda r: _announcement_dto(r[0], r[1], users))


@router.post("/{id}/announcements", summary="发作品公告（作者/管理）", status_code=201)
async def post_announcement(
    id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)
):
    body = _obj(body)
    title = _text(body.get("title")).strip()
    content = _text(body.get("body")).strip()
    fields = {}
    if not title or len(title) > ANNOUNCEMENT_TITLE_MAX:
        fields["title"] = f"标题 1-{ANNOUNCEMENT_TITLE_MAX} 个字"
    if not content or len(content) > TEXT_MAX:
        fields["body"] = f"内容 1-{TEXT_MAX} 字"
    if fields:
        raise invalid(fields)
    async with get_session_factory()() as session:
        work = await get_editable_work(session, ctx, id)
        row = WorkAnnouncement(
            work_id=work.id, author_id=viewer.user_id, title=title, body=content
        )
        session.add(row)
        await session.flush()
        seq = (
            await session.execute(
                select(func.count()).where(
                    WorkAnnouncement.work_id == work.id,
                    (WorkAnnouncement.created_at < row.created_at)
                    | (
                        (WorkAnnouncement.created_at == row.created_at)
                        & (WorkAnnouncement.id <= row.id)
                    ),
                )
            )
        ).scalar_one()
        # 发布者自己算已确认；关注者收到提醒（和 V2 发公告同一份 notification）
        await wx.set_ack(session, viewer.user_id, work.id, "announcement", str(seq))
        await wx.notify_announcement_followers(
            session, work.id, row, viewer.user_id, viewer.discord_user_id
        )
        work.updated_at = utc_now()
        users = await load_users(session, [viewer.user_id])
        result = _announcement_dto(row, seq, users)
        await session.commit()
        return result


# ---------------- 确认 ----------------


@router.post("/{id}/ack", summary="确认公告/规则/新版本", status_code=204)
async def ack(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    body = _obj(body)
    kind = body.get("kind")
    version = body.get("version")
    fields = {}
    if kind not in wx.ACK_KINDS:
        fields["kind"] = "kind 只能是 announcement、rules、version"
    if (
        isinstance(version, bool)
        or not isinstance(version, (str, int))
        or (isinstance(version, str) and not version.strip())
    ):
        fields["version"] = "缺少 version"
    if fields:
        raise invalid(fields)
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        await wx.set_ack(session, viewer.user_id, work.id, kind, str(version).strip())
        await session.commit()
    return Response(status_code=204)


# ---------------- 规则 ----------------


def _rules_dto(row: SiteWorkRules | None, work) -> dict:
    if row is None:
        return {
            "body": "",
            "version": 1,
            "updatedAt": iso_utc(work.created_at),
        }
    return {
        "body": row.body,
        "version": row.version,
        "updatedAt": iso_utc(row.updated_at),
    }


@router.get("/{id}/rules", summary="作品规则")
async def get_rules(id: str, ctx: Ctx):
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        row = await session.get(SiteWorkRules, work.id)
        return _rules_dto(row, work)


@router.put("/{id}/rules", summary="改作品规则（作者/管理）")
async def put_rules(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    body = _obj(body)
    if not isinstance(body.get("body"), str):
        raise invalid({"body": "缺少规则正文"})
    text = _text(body["body"])
    if len(text) > TEXT_MAX:
        raise invalid({"body": f"规则最多 {TEXT_MAX} 字"})
    async with get_session_factory()() as session:
        work = await get_editable_work(session, ctx, id)
        row = await session.get(SiteWorkRules, work.id, with_for_update=True)
        now = utc_now()
        if row is None:
            # 没有行时视为版本 1 的空规则，第一次保存就是版本 2
            row = SiteWorkRules(work_id=work.id, body=text, version=2, updated_at=now)
            session.add(row)
        else:
            row.body = text
            row.version += 1
            row.updated_at = now
        await wx.set_ack(session, viewer.user_id, work.id, "rules", str(row.version))
        await wx.notify_work_update(session, work, viewer.user_id, "更新了交流区规则")
        work.updated_at = now
        await session.flush()
        result = _rules_dto(row, work)
        await session.commit()
        return result


# ---------------- 钉选 ----------------


def _pin_dto(pin: SiteWorkPin, users: dict) -> dict:
    return {
        "id": str(pin.id),
        "workId": str(pin.work_id),
        "excerpt": pin.excerpt,
        "author": users.get(pin.author_id) if pin.author_id else None,
        "messageId": pin.message_id,
        "pinnedBy": pin.pinned_by,
        "createdAt": iso_utc(pin.created_at),
    }


@router.get("/{id}/pins", summary="钉选（新的在前）")
async def list_pins(id: str, ctx: Ctx, page: PageRequest = Depends(pagination)):
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        pins = (
            (
                await session.execute(
                    select(SiteWorkPin)
                    .where(SiteWorkPin.work_id == work.id)
                    .order_by(SiteWorkPin.created_at.desc(), SiteWorkPin.id.desc())
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        users = await load_users(session, [p.author_id for p in pins if p.author_id])
    return page_response(pins, page, lambda p: _pin_dto(p, users))


@router.post("/{id}/pins", summary="钉选一条聊天消息（作者/管理）", status_code=201)
async def post_pin(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    body = _obj(body)
    excerpt = _text(body.get("excerpt")).strip()[:PIN_EXCERPT_MAX]
    if not excerpt:
        raise invalid({"excerpt": "缺少消息摘要"})
    message_id = body.get("messageId")
    if message_id is not None and (
        not isinstance(message_id, str) or len(message_id) > 64
    ):
        raise invalid({"messageId": "消息 ID 不对"})
    author_id = body.get("authorId")
    if author_id is not None and (
        not isinstance(author_id, str) or len(author_id) > 64
    ):
        raise invalid({"authorId": "用户 ID 不对"})
    async with get_session_factory()() as session:
        work = await get_editable_work(session, ctx, id)
        count = (
            await session.execute(
                select(func.count()).where(SiteWorkPin.work_id == work.id)
            )
        ).scalar_one()
        if count >= PIN_MAX:
            raise invalid({"excerpt": f"最多钉选 {PIN_MAX} 条"})
        if message_id:
            dup = (
                await session.execute(
                    select(SiteWorkPin.id).where(
                        SiteWorkPin.work_id == work.id,
                        SiteWorkPin.message_id == message_id,
                    )
                )
            ).first()
            if dup:
                raise invalid({"messageId": "这条消息已经钉选过了"})
        if author_id and await session.get(User, author_id) is None:
            author_id = None
        pin = SiteWorkPin(
            work_id=work.id,
            excerpt=excerpt,
            author_id=author_id,
            message_id=message_id,
            pinned_by=viewer.user_id,
        )
        session.add(pin)
        await session.flush()
        users = await load_users(session, [author_id] if author_id else [])
        result = _pin_dto(pin, users)
        await session.commit()
        return result


@router.delete("/{id}/pins/{pinId}", summary="取消钉选（作者/管理）", status_code=204)
async def delete_pin(id: str, pinId: str, viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        work = await get_editable_work(session, ctx, id)
        pin = await session.get(SiteWorkPin, int(pinId)) if pinId.isdigit() else None
        if pin is None or pin.work_id != work.id:
            raise not_found()
        await session.delete(pin)
        await session.commit()
    return Response(status_code=204)


# ---------------- 关注 ----------------


async def _follow(id: str, viewer, ctx, on: bool) -> Response:
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        await wx.set_work_follow(session, work, viewer, on)
        await session.commit()
    return Response(status_code=204)


@router.post("/{id}/follow", summary="关注作品更新", status_code=204)
async def follow_work(id: str, viewer: CurrentUser, ctx: Ctx):
    return await _follow(id, viewer, ctx, True)


@router.delete("/{id}/follow", summary="取消关注作品", status_code=204)
async def unfollow_work(id: str, viewer: CurrentUser, ctx: Ctx):
    return await _follow(id, viewer, ctx, False)


# ---------------- DC 原帖存档 ----------------


def _can_hide(work, viewer) -> bool:
    return viewer is not None and (
        can_manage_work(work, viewer) or viewer.has_permission("moderation.content")
    )


def _archive_dto(post: SiteDcArchivePost, ctx, users: dict, show_hidden: bool) -> dict:
    visible = ctx.visible_ratings()
    attachments = []
    for i, a in enumerate(post.attachments or []):
        rating = a.get("rating") or post.rating or "general"
        url = a.get("url") if rating in visible else None
        att = external_image(
            str(a.get("id") or f"dca_{post.id}_{i}"),
            url,
            a.get("name") or "image",
            rating,
        )
        att["placeholder"] = url is None and rating not in visible
        att["mime"] = a.get("mime") or att["mime"]
        att["size"] = int(a.get("size") or 0)
        if not str(att["mime"]).startswith("image/"):
            att["kind"] = "file"
        attachments.append(att)
    hidden = post.hidden_state
    return {
        "id": str(post.id),
        "legacyMessageId": post.legacy_message_id,
        "author": users.get(post.author_id) if post.author_id else None,
        "legacyAuthorName": post.legacy_author_name,
        "body": post.body if (hidden is None or show_hidden) else "",
        "attachments": attachments if (hidden is None or show_hidden) else [],
        "legacyReactions": [
            {"emoji": str(r.get("emoji")), "count": int(r.get("count") or 0)}
            for r in (post.reactions or [])
        ],
        "replyTo": post.reply_to,
        "createdAt": iso_utc(post.created_at),
        "hiddenState": hidden,
    }


@router.get("/{id}/dc-archive", summary="DC 原帖讨论的只读存档（按原发帖时间正序）")
async def dc_archive(id: str, ctx: Ctx, page: PageRequest = Depends(pagination)):
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        if work.source_platform != "discord":
            raise not_found()
        show_hidden = _can_hide(work, ctx.viewer)
        q = select(SiteDcArchivePost).where(SiteDcArchivePost.work_id == work.id)
        if not show_hidden:
            q = q.where(SiteDcArchivePost.hidden_state.is_(None))
        posts = (
            (
                await session.execute(
                    q.order_by(SiteDcArchivePost.created_at, SiteDcArchivePost.id)
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        users = await load_users(session, [p.author_id for p in posts if p.author_id])
    return page_response(
        posts, page, lambda p: _archive_dto(p, ctx, users, show_hidden)
    )


async def _toggle_archive(id: str, post_id: str, viewer, ctx, hide: bool) -> Response:
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        if work.source_platform != "discord":
            raise not_found()
        if not _can_hide(work, viewer):
            raise forbidden("只有作者、卡贴管理员和站务可以隐藏存档楼层")
        post = (
            await session.get(SiteDcArchivePost, int(post_id), with_for_update=True)
            if post_id.isdigit()
            else None
        )
        if post is None or post.work_id != work.id:
            raise not_found()
        if hide:
            post.hidden_state = (
                "by_author"
                if work.author_id == viewer.user_id
                else "by_manager"
                if can_manage_work(work, viewer)
                else "by_staff"
            )
        else:
            post.hidden_state = None
        await session.commit()
    return Response(status_code=204)


@router.post("/{id}/dc-archive/{postId}/hide", summary="隐藏存档楼层", status_code=204)
async def hide_archive_post(id: str, postId: str, viewer: CurrentUser, ctx: Ctx):
    return await _toggle_archive(id, postId, viewer, ctx, True)


@router.post(
    "/{id}/dc-archive/{postId}/unhide", summary="取消隐藏存档楼层", status_code=204
)
async def unhide_archive_post(id: str, postId: str, viewer: CurrentUser, ctx: Ctx):
    return await _toggle_archive(id, postId, viewer, ctx, False)
