from identity.principal import current_identity_id
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select

from api.v1.dependencies.security import get_current_user, require_auth
from api.v1.schemas.notification import (
    MarkReadResponse,
    NotificationItem,
    NotificationList,
    UnreadCountResponse,
)
from api.v1.schemas.search import LatestUpdate
from api.v1.utils import ThreadDetailBuilder
from core.notification_repository import NotificationRepository
from core.thread_presentation_service import ThreadPresentationService
from core.thread_repository import ThreadRepository
from models import ThreadUpdate, Thread
from models.work_announcement import WorkAnnouncement
from sqlalchemy import or_
from shared.database import AsyncSessionFactory

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/notifications",
    tags=["动态通知"],
    dependencies=[Depends(require_auth)],
)


@router.get("", response_model=NotificationList, summary="获取动态通知列表")
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = Query(default=False),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> NotificationList:
    """分页返回作品更新与作者新作通知。"""
    user_id = current_identity_id(current_user)
    async with AsyncSessionFactory() as session:
        notification_repository = NotificationRepository(session)
        (
            notifications,
            total,
            unread_count,
        ) = await notification_repository.list_for_user(
            user_id, unread_only, limit, offset, legacy_visible=True
        )
        work_ids = {item.work_id for item in notifications if item.work_id is not None}
        discord_by_work = {}
        if work_ids:
            for work_id, external_id in await session.execute(
                select(Thread.work_id, Thread.thread_id)
                .where(
                    Thread.work_id.in_(work_ids),
                    Thread.platform == "discord",
                    Thread.deleted_at.is_(None),
                    Thread.show_flag.is_(True),
                )
                .order_by(Thread.id)
            ):
                discord_by_work.setdefault(work_id, external_id)
        notice_threads = {
            item.id: (
                discord_by_work.get(item.work_id)
                if item.event_type == "work_announcement"
                else item.thread_id
            )
            for item in notifications
        }
        thread_ids = list(
            {value for value in notice_threads.values() if value is not None}
        )
        threads = await ThreadRepository(session).get_threads_by_ids_with_tags(
            thread_ids
        )
        thread_map = {thread.thread_id: thread for thread in threads}
        context = await ThreadPresentationService(session).load(user_id, threads)
        thread_builder = ThreadDetailBuilder()

        update_ids = {
            item.event_source_id
            for item in notifications
            if item.event_type == "thread_update"
        }
        update_map: dict[int, ThreadUpdate] = {}
        if update_ids:
            statement = select(ThreadUpdate).where(ThreadUpdate.id.in_(update_ids))
            update_map = {
                update.id: update
                for update in (await session.execute(statement)).scalars().all()
                if update.id is not None
            }

        announcement_ids = {
            item.event_source_id
            for item in notifications
            if item.event_type == "work_announcement"
        }
        announcement_map = {}
        legacy_announcements = {}
        if announcement_ids or update_ids:
            for announcement in (
                await session.execute(
                    select(WorkAnnouncement).where(
                        or_(
                            WorkAnnouncement.id.in_(announcement_ids),
                            WorkAnnouncement.legacy_update_id.in_(update_ids),
                        )
                    )
                )
            ).scalars():
                announcement_map[announcement.id] = announcement
                if announcement.legacy_update_id is not None:
                    legacy_announcements[announcement.legacy_update_id] = announcement

        results: list[NotificationItem] = []
        for item in notifications:
            if item.event_type == "tag_review":
                results.append(
                    NotificationItem(
                        id=item.id,
                        type="tag_review",
                        target_type=item.target_type,
                        target_id=str(item.target_id),
                        proposal_id=str(item.event_source_id),
                        created_at=item.created_at,
                        read_at=item.read_at,
                    )
                )
                continue
            if (
                item.id is None
                or (thread := thread_map.get(notice_threads.get(item.id))) is None
            ):
                continue
            update_response = None
            update_record = (
                update_map.get(item.event_source_id)
                if item.event_type == "thread_update"
                else None
            )
            announcement = (
                announcement_map.get(item.event_source_id)
                if item.event_type == "work_announcement"
                else legacy_announcements.get(item.event_source_id)
                if item.event_type == "thread_update"
                else None
            )
            if update_record is not None:
                message_link = None
                if update_record.message_id is not None:
                    message_link = (
                        f"https://discord.com/channels/{thread.guild_id}/"
                        f"{thread.thread_id}/{update_record.message_id}"
                    )
                update_response = LatestUpdate(
                    id=update_record.id,
                    description=update_record.description,
                    version=update_record.version,
                    message_link=message_link,
                    source_message_at=update_record.source_message_at,
                    published_at=update_record.published_at,
                )
            if announcement is not None and (
                item.event_type == "work_announcement" or announcement.withdrawn_at
            ):
                update_response = LatestUpdate(
                    id=announcement.id,
                    description="公告已撤回"
                    if announcement.withdrawn_at
                    else announcement.body[:500],
                    version=None
                    if announcement.withdrawn_at
                    else (announcement.title or "")[:50] or None,
                    message_link=None,
                    source_message_at=announcement.created_at,
                    published_at=announcement.created_at,
                )
            results.append(
                NotificationItem(
                    id=item.id,
                    type="thread_update"
                    if item.event_type == "work_announcement"
                    else item.event_type,
                    thread=thread_builder.build(
                        thread,
                        set(),
                        presentation_context=context,
                    ),
                    update=update_response,
                    created_at=item.created_at,
                    read_at=item.read_at,
                )
            )
    return NotificationList(
        results=results,
        total=total,
        unread_count=unread_count,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    summary="获取动态通知未读数",
)
async def get_unread_count(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> UnreadCountResponse:
    """返回当前用户全部动态通知的未读数量。"""
    async with AsyncSessionFactory() as session:
        count = await NotificationRepository(session).unread_count(
            current_identity_id(current_user), legacy_visible=True
        )
    return UnreadCountResponse(unread_count=count)


@router.post(
    "/threads/{thread_id}/read",
    response_model=MarkReadResponse,
    summary="按作品标记动态通知已读",
)
async def mark_thread_read(
    thread_id: int,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> MarkReadResponse:
    """幂等标记用户在指定作品下的全部动态通知。"""
    async with AsyncSessionFactory() as session:
        count = await NotificationRepository(session).mark_thread_read(
            current_identity_id(current_user), thread_id, legacy_visible=True
        )
        await session.commit()
    return MarkReadResponse(thread_id=thread_id, marked_read=count)


@router.post(
    "/read-all",
    response_model=MarkReadResponse,
    summary="将全部动态通知标为已读",
)
async def mark_all_read(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> MarkReadResponse:
    """幂等标记当前用户的全部动态通知。"""
    async with AsyncSessionFactory() as session:
        count = await NotificationRepository(session).mark_all_read(
            current_identity_id(current_user), legacy_visible=True
        )
        await session.commit()
    return MarkReadResponse(marked_read=count)


@router.post(
    "/{notification_id}/read",
    response_model=MarkReadResponse,
    summary="标记单条通知已读",
)
async def mark_one_read(
    notification_id: Annotated[
        int,
        Path(
            description="要标记已读的通知内部 ID，取自通知列表的 id；仅可操作当前用户自己的通知"
        ),
    ],
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """读取书单审核等不关联作品的通知时可单独标记。"""
    async with AsyncSessionFactory() as session:
        count = await NotificationRepository(session).mark_one_read(
            current_identity_id(current_user), notification_id
        )
        await session.commit()
    return MarkReadResponse(marked_read=count)
