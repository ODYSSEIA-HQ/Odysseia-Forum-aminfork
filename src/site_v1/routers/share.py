"""协议 3.20 分享预览（草案 J）：给边缘函数生成 Open Graph 卡片 {title, description, image, url}。

一律按未登录（全年龄模式）计算，不看请求带的会话（对齐假后端 handlers/share.ts）：
- 受限作品 404；audience=r18 的活动、草稿活动 404；私密书单 404；
- image 只用全年龄图，没有就是 null；
- 用户、书单、活动的描述里只数、只列全年龄作品。
url 用 SITE_FRONTEND_BASE（没配时用请求的站点根）。
"""

import re

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from models.booklist import Booklist
from models.booklist_item import BooklistItem
from models.user import User
from models.work import Work
from site_v1.config import get_config
from site_v1.context import SiteContext
from site_v1.deps import get_session_factory
from site_v1.dto import avatar_dto
from site_v1.errors import not_found
from site_v1.services import events as ev
from site_v1.services.attachments import attachment_dto, load_attachments
from site_v1.services.work_access import parse_id, work_visible
from site_v1.services.works import listable_conditions

router = APIRouter(tags=["分享预览"])


def anon() -> SiteContext:
    """未登录、全年龄模式的上下文。"""
    return SiteContext()


def origin(request: Request) -> str:
    base = get_config().frontend_base
    return base or str(request.base_url).rstrip("/")


def clip(text: str | None, limit: int = 160) -> str:
    plain = re.sub(r"\s+", " ", re.sub(r"[#>*_`\[\]()!-]", " ", text or "")).strip()
    return plain if len(plain) <= limit else plain[: limit - 1] + "…"


async def general_image(session, attachment_id) -> str | None:
    if not attachment_id:
        return None
    att = (await load_attachments(session, [attachment_id])).get(int(attachment_id))
    if att is None or (att.rating or "general") != "general" or att.state != "ready":
        return None
    return attachment_dto(att, anon())["url"]


@router.get("/share/works/{id}", summary="作品的 Open Graph 卡片")
async def share_work(id: str, request: Request):
    ctx = anon()
    async with get_session_factory()() as session:
        work_id = parse_id(id)
        work = await session.get(Work, work_id) if work_id else None
        if work is None or work.status != "published" or not await work_visible(session, ctx, work):
            raise not_found()
        return {
            "title": work.title,
            "description": clip(work.summary or work.description),
            "image": await general_image(session, work.cover_attachment_id),
            "url": f"{origin(request)}/works/{work.id}",
        }


@router.get("/share/users/{id}", summary="用户的 Open Graph 卡片")
async def share_user(id: str, request: Request):
    ctx = anon()
    async with get_session_factory()() as session:
        user = await session.get(User, id)
        if user is None or user.merged_into_id:
            raise not_found()
        conds = await listable_conditions(session, ctx)
        base = select(Work).where(Work.author_id == user.id, *conds)
        count = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        titles = (
            await session.execute(
                select(Work.title)
                .where(Work.author_id == user.id, *conds)
                .order_by(Work.published_at.desc().nulls_last(), Work.id.desc())
                .limit(3)
            )
        ).scalars().all()
        listed = "".join(f"「{t}」" for t in titles)
        avatar = avatar_dto(user.id, user.avatar_url)
        return {
            "title": user.display_name or user.username,
            "description": f"{count} 个作品：{listed}{'等' if count > 3 else ''}" if count else "还没有公开作品",
            "image": avatar["url"] if avatar else None,
            "url": f"{origin(request)}/users/{user.id}",
        }


@router.get("/share/collections/{id}", summary="书单的 Open Graph 卡片")
async def share_collection(id: str, request: Request):
    ctx = anon()
    async with get_session_factory()() as session:
        booklist_id = parse_id(id)
        c = await session.get(Booklist, booklist_id) if booklist_id else None
        if c is None or not c.is_public or c.merged_into_id:
            raise not_found()
        conds = await listable_conditions(session, ctx)
        rows = (
            await session.execute(
                select(Work.id, Work.cover_attachment_id)
                .join(BooklistItem, BooklistItem.work_id == Work.id)
                .where(BooklistItem.booklist_id == c.id, *conds)
                .order_by(BooklistItem.display_order, BooklistItem.id)
            )
        ).all()
        image = None
        for _, cover_id in rows:
            image = await general_image(session, cover_id)
            if image:
                break
        # 书单自己的封面（V2 的 cover_image_url）没有分级信息，不用
        return {
            "title": f"书单：{c.title}",
            "description": clip(f"{len(rows)} 个作品。{c.description or ''}"),
            "image": image,
            "url": f"{origin(request)}/booklists/{c.id}",
        }


@router.get("/share/events/{id}", summary="活动的 Open Graph 卡片")
async def share_event(id: str, request: Request):
    ctx = anon()
    async with get_session_factory()() as session:
        e = await ev.find_event(session, id)
        if not ev.event_visible(e, ctx):
            raise not_found()
        count = len((await ev.public_entries_by_event(session, ctx, [e]))[e.id])
        return {
            "title": f"活动：{e.title}",
            "description": clip(f"{e.summary}（{count} 件参赛作品）"),
            "image": await general_image(session, e.cover_attachment_id),
            "url": f"{origin(request)}/events/{e.id}",
        }
