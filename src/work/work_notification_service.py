"""新旧通知共用事实、分页和分类可见性。"""

from sqlalchemy import select, update, func, and_, or_, case
from sqlalchemy.orm import aliased
from dto.work.work_command_qo import WorkCommandQo
from shared.time_utils import utc_now
from models.notification import Notification
from models.thread import Thread
from models.work import Work
from models.work_announcement import WorkAnnouncement
from models.thread_update import ThreadUpdate


class WorkNotificationService:
    """读取同一通知行，兼容旧更新、作者新作及标签审核。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂。"""
        self.sessions = sessions

    async def notifications(self, offset: int, limit: int, command: WorkCommandQo):
        """关联旧来源与公告，分类过滤同时作用于记录、总数及未读数。"""
        principal = command.actor
        offset, limit = max(0, offset), min(100, max(1, limit))
        async with self.sessions() as session:
            recipient = or_(
                Notification.fluxer_user_id == principal["id"],
                and_(
                    Notification.fluxer_user_id.is_(None),
                    Notification.user_id == int(principal["discord_user_id"]),
                ),
            )
            legacy_work = (
                select(Thread.work_id)
                .where(
                    Thread.thread_id
                    == func.coalesce(
                        Notification.thread_id,
                        case(
                            (
                                Notification.target_type == "thread",
                                Notification.target_id,
                            )
                        ),
                    ),
                    Thread.platform == Notification.event_source_platform,
                )
                .order_by(Thread.id)
                .limit(1)
                .correlate(Notification)
                .scalar_subquery()
            )
            work_id = func.coalesce(
                Notification.work_id,
                WorkAnnouncement.work_id,
                ThreadUpdate.work_id,
                case((Notification.target_type == "work", Notification.target_id)),
                legacy_work,
            )
            duplicate = aliased(Notification)
            legacy_duplicate = (
                select(duplicate.id)
                .where(
                    duplicate.event_type == "thread_update",
                    duplicate.event_source_platform == "discord",
                    duplicate.event_source_id == WorkAnnouncement.legacy_update_id,
                    or_(
                        and_(
                            duplicate.fluxer_user_id.is_not(None),
                            duplicate.fluxer_user_id == Notification.fluxer_user_id,
                        ),
                        and_(
                            duplicate.user_id.is_not(None),
                            duplicate.user_id == Notification.user_id,
                        ),
                    ),
                )
                .correlate(Notification, WorkAnnouncement)
                .exists()
            )
            abyss_config = principal.get("config", {}).get("abyss")
            abyss_column = Work.is_abyss
            if abyss_config is not None:
                channels = [str(value) for value in abyss_config.get("channel_ids", [])]
                abyss_column = case(
                    (
                        Work.source_platform == "discord",
                        Work.source_channel_id.in_(channels),
                    ),
                    else_=Work.is_abyss,
                )
            base = (
                select(Notification, WorkAnnouncement, ThreadUpdate, Work)
                .outerjoin(
                    WorkAnnouncement,
                    or_(
                        and_(
                            Notification.event_type == "work_announcement",
                            WorkAnnouncement.id == Notification.event_source_id,
                        ),
                        and_(
                            Notification.event_type == "thread_update",
                            Notification.event_source_platform == "discord",
                            WorkAnnouncement.legacy_update_id
                            == Notification.event_source_id,
                        ),
                    ),
                )
                .outerjoin(
                    ThreadUpdate,
                    and_(
                        Notification.event_type == "thread_update",
                        Notification.event_source_platform == "discord",
                        ThreadUpdate.id == Notification.event_source_id,
                    ),
                )
                .outerjoin(Work, Work.id == work_id)
                .where(
                    recipient,
                    or_(
                        and_(
                            Notification.event_type == "tag_review",
                            Notification.target_type == "booklist",
                            work_id.is_(None),
                        ),
                        and_(
                            Work.id.is_not(None),
                            Work.deleted_at.is_(None),
                            Work.source_visible.is_(True),
                            or_(Work.is_nsfw.is_(False), principal["show_nsfw"]),
                            or_(abyss_column.is_(False), principal["can_view_abyss"]),
                        ),
                    ),
                    ~and_(
                        Notification.event_type == "work_announcement",
                        WorkAnnouncement.legacy_update_id.is_not(None),
                        legacy_duplicate,
                    ),
                )
            )
            total = (
                await session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
            unread = (
                await session.execute(
                    select(func.count()).select_from(
                        base.where(Notification.read_at.is_(None)).subquery()
                    )
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    base.order_by(
                        Notification.created_at.desc(), Notification.id.desc()
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            items = []
            for notice, announcement, legacy, work in rows:
                withdrawn = bool(announcement and announcement.withdrawn_at)
                title = (
                    announcement.title
                    if announcement
                    else (
                        legacy.version
                        if legacy
                        else (work.title if work else "标签审核")
                    )
                )
                body = (
                    announcement.body
                    if announcement
                    else (
                        legacy.description
                        if legacy
                        else (
                            "关注的作者发布了新作品"
                            if notice.event_type == "author_new_thread"
                            else "有新的标签申请待审核"
                        )
                    )
                )
                items.append(
                    {
                        "id": str(notice.id),
                        "type": notice.event_type,
                        "work_id": str(work.id) if work else None,
                        "announcement_id": str(announcement.id)
                        if announcement
                        else None,
                        "title": None if withdrawn else title,
                        "body": "公告已撤回" if withdrawn else body,
                        "withdrawn": withdrawn,
                        "target_type": notice.target_type,
                        "target_id": str(notice.target_id)
                        if notice.target_id is not None
                        else None,
                        "proposal_id": str(notice.event_source_id)
                        if notice.event_type == "tag_review"
                        else None,
                        "read_at": notice.read_at,
                        "created_at": notice.created_at,
                    }
                )
            return {
                "items": items,
                "total": total,
                "unread_count": unread,
                "offset": offset,
                "limit": limit,
            }

    async def read_notification(self, notification_id: int, command: WorkCommandQo):
        """只更新当前接收者原始通知行，与 V1 共享已读时间。"""
        principal = command.actor
        async with self.sessions() as session:
            await session.execute(
                update(Notification)
                .where(
                    Notification.id == notification_id,
                    or_(
                        Notification.fluxer_user_id == principal["id"],
                        and_(
                            Notification.fluxer_user_id.is_(None),
                            Notification.user_id == int(principal["discord_user_id"]),
                        ),
                    ),
                    Notification.read_at.is_(None),
                )
                .values(read_at=utc_now())
            )
            await session.commit()
        return {"read": True}
