from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from models import AuthorFollow, Notification, ThreadFollow
from identity.runtime import uses_canonical_storage
from identity.business_identity import resolve_business_id
from shared.time_utils import utc_now


class NotificationFanoutService:
    """通过数据库内查询为关注者批量生成动态通知。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def fanout_thread_update(
        self,
        thread_id: int,
        publisher_id: int,
        event_source_id: int,
        created_at: datetime,
    ) -> int:
        """为作品更新生成全部活跃帖子关注者通知。"""
        publisher_id = await resolve_business_id(self.session, publisher_id)
        recipient_id = (
            ThreadFollow.fluxer_user_id
            if uses_canonical_storage()
            else ThreadFollow.user_id
        )
        # 仅在数据库中选择活跃关注者，并排除更新发布者
        recipient_query = select(recipient_id.label("user_id")).where(
            ThreadFollow.thread_id == thread_id,
            ThreadFollow.active_flag.is_(True),
            recipient_id != publisher_id,
        )
        return await self._fanout(
            recipient_query=recipient_query,
            event_type="thread_update",
            event_source_id=event_source_id,
            thread_id=thread_id,
            created_at=created_at,
        )

    async def fanout_author_new_thread(
        self,
        author_id: int,
        thread_id: int,
        event_source_id: int,
        created_at: datetime | None = None,
    ) -> int:
        """为作者新作品生成全部活跃作者关注者通知。"""
        author_id = await resolve_business_id(self.session, author_id)
        recipient_id = (
            AuthorFollow.fluxer_user_id
            if uses_canonical_storage()
            else AuthorFollow.user_id
        )
        followed_author = (
            AuthorFollow.fluxer_author_id
            if uses_canonical_storage()
            else AuthorFollow.author_id
        )
        # 仅在数据库中选择活跃关注者，并排除作者本人
        recipient_query = select(recipient_id.label("user_id")).where(
            followed_author == author_id,
            AuthorFollow.active_flag.is_(True),
            recipient_id != author_id,
        )
        return await self._fanout(
            recipient_query=recipient_query,
            event_type="author_new_thread",
            event_source_id=event_source_id,
            thread_id=thread_id,
            created_at=created_at or utc_now(),
        )

    async def fanout_work_created(
        self,
        work_id: int,
        thread_id: int,
        author_id: str,
        platform: str,
        created_at: datetime,
    ) -> int:
        """按原生实际创建时间快照作者关注者，同事务去重且绝不补发后来关注者。"""
        from models.discord_identity import DiscordIdentity
        from sqlalchemy import or_, cast

        author_discord = (
            select(cast(DiscordIdentity.discord_user_id, BigInteger))
            .where(
                DiscordIdentity.user_id == author_id,
                DiscordIdentity.is_current.is_(True),
            )
            .scalar_subquery()
        )
        recipients = select(
            AuthorFollow.user_id,
            AuthorFollow.fluxer_user_id,
            literal("author_new_thread"),
            literal(thread_id, type_=BigInteger()),
            literal(platform),
            literal(thread_id, type_=BigInteger()),
            literal(work_id, type_=BigInteger()),
            literal(created_at, type_=DateTime()),
        ).where(
            or_(
                AuthorFollow.fluxer_author_id == author_id,
                AuthorFollow.author_id == author_discord,
            ),
            AuthorFollow.active_flag.is_(True),
            AuthorFollow.followed_at <= created_at,
            or_(
                AuthorFollow.fluxer_user_id.is_(None),
                AuthorFollow.fluxer_user_id != author_id,
            ),
            or_(
                AuthorFollow.user_id.is_(None),
                AuthorFollow.user_id != author_discord,
                author_discord.is_(None),
            ),
        )
        result = await self.session.execute(
            insert(Notification)
            .from_select(
                [
                    "user_id",
                    "fluxer_user_id",
                    "event_type",
                    "event_source_id",
                    "event_source_platform",
                    "thread_id",
                    "work_id",
                    "created_at",
                ],
                recipients,
            )
            .on_conflict_do_nothing()
        )
        return int(result.rowcount or 0)

    async def _fanout(
        self,
        recipient_query: Select[tuple[int]],
        event_type: str,
        event_source_id: int,
        thread_id: int,
        created_at: datetime,
    ) -> int:
        """将接收者查询直接转换为幂等通知插入语句。"""
        recipient_subquery = recipient_query.subquery()

        # 使用固定数量的绑定参数构造通知数据，避免关注者数量影响参数数量
        notification_rows = select(
            recipient_subquery.c.user_id,
            literal(event_type, type_=String(32)),
            literal(event_source_id, type_=BigInteger()),
            literal(thread_id, type_=BigInteger()),
            literal(created_at, type_=DateTime()),
        )
        statement = (
            insert(Notification)
            .from_select(
                [
                    "fluxer_user_id" if uses_canonical_storage() else "user_id",
                    "event_type",
                    "event_source_id",
                    "thread_id",
                    "created_at",
                ],
                notification_rows,
            )
            .on_conflict_do_nothing()
        )
        result = await self.session.execute(statement)
        return int(result.rowcount or 0)
