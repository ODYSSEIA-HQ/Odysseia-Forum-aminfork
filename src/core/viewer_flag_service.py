from sqlalchemy import select
from identity.runtime import uses_canonical_storage
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from dto.viewer_flag_map import ViewerFlagMap
from models import (
    AuthorFollow,
    Notification,
    ThreadFollow,
    BooklistItem,
    UserCollection,
)
from shared.enum import CollectionType


class ViewerFlagService:
    """批量聚合当前查看者与一组作品的关系标记。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_flags(
        self,
        user_id: int | None,
        thread_author_pairs: list[tuple[int, int]],
    ) -> ViewerFlagMap:
        """以固定次数查询返回每个作品的查看者标记。"""
        values = {thread_id: set() for thread_id, _ in thread_author_pairs}
        if user_id is None or not thread_author_pairs:
            return ViewerFlagMap(values=values)
        thread_ids = {thread_id for thread_id, _ in thread_author_pairs}
        author_ids = {author_id for _, author_id in thread_author_pairs}
        if uses_canonical_storage():
            from identity.business_identity import (
                resolve_business_id,
                resolve_business_ids,
            )

            user_id = await resolve_business_id(self.session, user_id)
            mapped = await resolve_business_ids(self.session, list(author_ids))
            canonical_authors = dict(zip(list(author_ids), mapped))
            thread_author_pairs = [
                (thread, canonical_authors[author])
                for thread, author in thread_author_pairs
            ]
            author_ids = set(mapped)

        if uses_canonical_storage():
            collected_statement = select(col(BooklistItem.thread_id)).where(
                col(BooklistItem.fluxer_owner_id) == user_id,
                col(BooklistItem.thread_id).in_(thread_ids),
            )
        else:
            collected_statement = select(col(UserCollection.target_id)).where(
                col(UserCollection.user_id) == user_id,
                col(UserCollection.target_type) == CollectionType.THREAD.value,
                col(UserCollection.target_id).in_(thread_ids),
            )
        collected_ids = set(
            (await self.session.execute(collected_statement)).scalars().all()
        )
        for thread_id in collected_ids:
            values[int(thread_id)].add("collected")

        followed_statement = select(col(ThreadFollow.thread_id)).where(
            col(
                ThreadFollow.fluxer_user_id
                if uses_canonical_storage()
                else ThreadFollow.user_id
            )
            == user_id,
            col(ThreadFollow.active_flag).is_(True),
            col(ThreadFollow.thread_id).in_(thread_ids),
        )
        followed_ids = set(
            (await self.session.execute(followed_statement)).scalars().all()
        )
        for thread_id in followed_ids:
            values[int(thread_id)].add("followed")

        author_statement = select(
            col(
                AuthorFollow.fluxer_author_id
                if uses_canonical_storage()
                else AuthorFollow.author_id
            )
        ).where(
            col(
                AuthorFollow.fluxer_user_id
                if uses_canonical_storage()
                else AuthorFollow.user_id
            )
            == user_id,
            col(AuthorFollow.active_flag).is_(True),
            col(
                AuthorFollow.fluxer_author_id
                if uses_canonical_storage()
                else AuthorFollow.author_id
            ).in_(author_ids),
        )
        followed_author_ids = set(
            (await self.session.execute(author_statement)).scalars().all()
        )
        for thread_id, author_id in thread_author_pairs:
            if author_id in followed_author_ids:
                values[thread_id].add("followed_author")

        unread_statement = (
            select(col(Notification.thread_id))
            .where(
                col(
                    Notification.fluxer_user_id
                    if uses_canonical_storage()
                    else Notification.user_id
                )
                == user_id,
                col(Notification.read_at).is_(None),
                col(Notification.thread_id).in_(thread_ids),
            )
            .distinct()
        )
        unread_ids = set((await self.session.execute(unread_statement)).scalars().all())
        for thread_id in unread_ids:
            values[int(thread_id)].add("unread")
        return ViewerFlagMap(values=values)
