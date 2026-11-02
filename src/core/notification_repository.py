from sqlalchemy import delete, func, select, update, and_, or_, cast, BigInteger
from identity.runtime import uses_canonical_storage
from identity.business_identity import resolve_business_id
from sqlalchemy.ext.asyncio import AsyncSession

from models import Notification
from shared.time_utils import utc_now


class NotificationRepository:
    """管理动态通知的单表数据库操作。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    def legacy_visible_condition(self):
        """V1 仅能表达真实 Discord 帖子和书单目标，列表与计数共用同一条件。"""
        from models.thread import Thread
        from models.work_announcement import WorkAnnouncement
        from models.work import Work

        discord_thread = (
            select(Thread.id)
            .where(
                Thread.platform == "discord",
                Thread.show_flag.is_(True),
                Thread.deleted_at.is_(None),
                Thread.thread_id == Notification.thread_id,
            )
            .correlate(Notification)
            .exists()
        )
        discord_target = (
            select(Thread.id)
            .where(
                Thread.platform == "discord",
                Thread.show_flag.is_(True),
                Thread.deleted_at.is_(None),
                Thread.thread_id == Notification.target_id,
            )
            .correlate(Notification)
            .exists()
        )
        announcement_work = (
            select(WorkAnnouncement.work_id)
            .where(WorkAnnouncement.id == Notification.event_source_id)
            .correlate(Notification)
            .scalar_subquery()
        )
        discord_work = (
            select(Thread.id)
            .join(Work, Work.id == Thread.work_id)
            .where(
                Thread.platform == "discord",
                Thread.show_flag.is_(True),
                Thread.deleted_at.is_(None),
                Work.deleted_at.is_(None),
                Work.source_visible.is_(True),
                Thread.work_id
                == func.coalesce(Notification.work_id, announcement_work),
            )
            .correlate(Notification)
            .exists()
        )
        from sqlalchemy.orm import aliased

        original = aliased(Notification)
        mirrored_update = (
            select(WorkAnnouncement.legacy_update_id)
            .where(WorkAnnouncement.id == Notification.event_source_id)
            .correlate(Notification)
            .scalar_subquery()
        )
        legacy_duplicate = (
            select(original.id)
            .where(
                original.event_type == "thread_update",
                original.event_source_platform == "discord",
                original.event_source_id == mirrored_update,
                or_(
                    and_(
                        original.user_id.is_not(None),
                        original.user_id == Notification.user_id,
                    ),
                    and_(
                        original.fluxer_user_id.is_not(None),
                        original.fluxer_user_id == Notification.fluxer_user_id,
                    ),
                ),
            )
            .correlate(Notification)
            .exists()
        )
        return or_(
            and_(
                Notification.event_type == "work_announcement",
                discord_work,
                ~legacy_duplicate,
            ),
            and_(
                Notification.event_source_platform == "discord",
                Notification.event_type.in_(["thread_update", "author_new_thread"]),
                discord_thread,
            ),
            and_(
                Notification.event_source_platform == "discord",
                Notification.event_type == "tag_review",
                or_(
                    Notification.target_type == "booklist",
                    and_(Notification.target_type == "thread", discord_target),
                ),
            ),
        )

    def recipient_condition(self, user_id):
        """旧协议允许读取仅有统一编号的 DC 可表达通知，回退必须经过当前权威绑定。"""
        from models.discord_identity import DiscordIdentity

        if uses_canonical_storage():
            discord = (
                select(cast(DiscordIdentity.discord_user_id, BigInteger))
                .where(
                    DiscordIdentity.user_id == str(user_id),
                    DiscordIdentity.is_current.is_(True),
                )
                .scalar_subquery()
            )
            return or_(
                Notification.fluxer_user_id == user_id,
                and_(
                    Notification.fluxer_user_id.is_(None),
                    Notification.user_id == discord,
                ),
            )
        canonical = (
            select(DiscordIdentity.user_id)
            .where(
                DiscordIdentity.discord_user_id == str(user_id),
                DiscordIdentity.is_current.is_(True),
            )
            .scalar_subquery()
        )
        return or_(
            Notification.user_id == user_id,
            and_(
                Notification.user_id.is_(None), Notification.fluxer_user_id == canonical
            ),
        )

    async def list_for_user(
        self,
        user_id: int,
        unread_only: bool,
        limit: int,
        offset: int,
        *,
        legacy_visible: bool = False,
    ) -> tuple[list[Notification], int, int]:
        """分页读取通知并同时返回总数与未读数。"""
        user_id = await resolve_business_id(self.session, user_id)
        conditions = [self.recipient_condition(user_id)]
        if legacy_visible:
            conditions.append(self.legacy_visible_condition())
        if unread_only:
            conditions.append(Notification.read_at.is_(None))
        statement = (
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .offset(offset)
        )
        total_statement = (
            select(func.count()).select_from(Notification).where(*conditions)
        )
        unread_statement = (
            select(func.count())
            .select_from(Notification)
            .where(
                self.recipient_condition(user_id),
                Notification.read_at.is_(None),
            )
        )
        if legacy_visible:
            unread_statement = unread_statement.where(self.legacy_visible_condition())
        rows = list((await self.session.execute(statement)).scalars().all())
        total = int((await self.session.execute(total_statement)).scalar_one())
        unread = int((await self.session.execute(unread_statement)).scalar_one())
        return rows, total, unread

    async def unread_count(self, user_id: int, *, legacy_visible: bool = False) -> int:
        """获取用户动态通知未读数。"""
        user_id = await resolve_business_id(self.session, user_id)
        statement = (
            select(func.count())
            .select_from(Notification)
            .where(
                self.recipient_condition(user_id),
                Notification.read_at.is_(None),
            )
        )
        if legacy_visible:
            statement = statement.where(self.legacy_visible_condition())
        return int((await self.session.execute(statement)).scalar_one())

    async def mark_thread_read(
        self, user_id: int, thread_id: int, *, legacy_visible: bool = False
    ) -> int:
        """将用户在某作品下的全部未读动态标为已读。"""
        user_id = await resolve_business_id(self.session, user_id)
        from models.thread import Thread

        works = select(Thread.work_id).where(
            Thread.platform == "discord", Thread.thread_id == thread_id
        )
        statement = (
            update(Notification)
            .where(
                self.recipient_condition(user_id),
                or_(
                    Notification.thread_id == thread_id,
                    and_(
                        Notification.event_type == "work_announcement",
                        Notification.work_id.in_(works),
                    ),
                ),
                Notification.read_at.is_(None),
            )
            .values(read_at=utc_now())
        )
        if legacy_visible:
            statement = statement.where(self.legacy_visible_condition())
        result = await self.session.execute(statement)
        return int(result.rowcount or 0)

    async def mark_one_read(self, user_id: int, notification_id: int) -> int:
        """仅标记当前用户拥有的单条通知。"""
        user_id = await resolve_business_id(self.session, user_id)
        result = await self.session.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                self.recipient_condition(user_id),
                Notification.read_at.is_(None),
            )
            .values(read_at=utc_now())
        )
        return int(result.rowcount or 0)

    async def mark_all_read(self, user_id: int, *, legacy_visible: bool = False) -> int:
        """将用户的全部未读动态标为已读。"""
        user_id = await resolve_business_id(self.session, user_id)
        statement = (
            update(Notification)
            .where(
                self.recipient_condition(user_id),
                Notification.read_at.is_(None),
            )
            .values(read_at=utc_now())
        )
        if legacy_visible:
            statement = statement.where(self.legacy_visible_condition())
        result = await self.session.execute(statement)
        return int(result.rowcount or 0)

    async def delete_for_event(
        self, event_type: str, event_source_id: int, *, platform: str = "discord"
    ) -> None:
        """已映射公告的旧更新撤回保留原通知，其余事件按平台删除。"""
        if event_type == "thread_update" and platform == "discord":
            from models.work_announcement import WorkAnnouncement

            result = await self.session.execute(
                update(WorkAnnouncement)
                .where(WorkAnnouncement.legacy_update_id == event_source_id)
                .values(
                    withdrawn_at=func.coalesce(WorkAnnouncement.withdrawn_at, utc_now())
                )
            )
            if result.rowcount:
                return
        await self.session.execute(
            delete(Notification).where(
                Notification.event_type == event_type,
                Notification.event_source_id == event_source_id,
                Notification.event_source_platform == platform,
            )
        )

    async def delete_for_threads(
        self, thread_ids: list[int], *, platform: str = "discord"
    ) -> None:
        """批量删除作品关联的全部通知。"""
        if thread_ids:
            await self.session.execute(
                delete(Notification).where(
                    Notification.thread_id.in_(thread_ids),
                    Notification.event_source_platform == platform,
                )
            )
