from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Author, AuthorFollow, User
from identity.runtime import uses_canonical_storage, is_fluxer_mode
from identity.business_identity import resolve_business_id, discord_id_for_business_id
from shared.time_utils import utc_now


class AuthorFollowRepository:
    """管理作者关注关系的单表写入与关联读取。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def set_active(
        self,
        user_id: int,
        author_id: int,
        active: bool,
    ) -> AuthorFollow | None:
        """幂等设置作者关注状态。"""
        user_id = await resolve_business_id(self.session, user_id)
        author_id = await resolve_business_id(self.session, author_id)
        statement = select(AuthorFollow).where(
            (
                AuthorFollow.fluxer_user_id
                if uses_canonical_storage()
                else AuthorFollow.user_id
            )
            == user_id,
            (
                AuthorFollow.fluxer_author_id
                if uses_canonical_storage()
                else AuthorFollow.author_id
            )
            == author_id,
        )
        relation = (await self.session.execute(statement)).scalar_one_or_none()
        if relation is None:
            if not active:
                return None
            if uses_canonical_storage():
                mappings = {}
                for canonical in (user_id, author_id):
                    original = await discord_id_for_business_id(self.session, canonical)
                    if original is not None:
                        mappings[str(canonical)] = str(original)
                relation = AuthorFollow(
                    user_id=int(mappings[str(user_id)])
                    if str(user_id) in mappings
                    else None,
                    author_id=int(mappings[str(author_id)])
                    if str(author_id) in mappings
                    else None,
                    fluxer_user_id=str(user_id),
                    fluxer_author_id=str(author_id),
                )
            else:
                relation = AuthorFollow(user_id=user_id, author_id=author_id)
            self.session.add(relation)
            await self.session.flush()
            return relation

        if active and not relation.active_flag:
            relation.followed_at = utc_now()
        relation.active_flag = active
        self.session.add(relation)
        await self.session.flush()
        return relation

    async def list_for_user(
        self,
        user_id: int,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[AuthorFollow, Author]], int]:
        """分页返回用户关注的作者及作者资料。"""
        user_id = await resolve_business_id(self.session, user_id)
        conditions = [
            (
                AuthorFollow.fluxer_user_id
                if uses_canonical_storage()
                else AuthorFollow.user_id
            )
            == user_id
        ]
        if uses_canonical_storage() and not is_fluxer_mode():
            conditions.append(AuthorFollow.author_id.is_not(None))
        if active is not None:
            conditions.append(AuthorFollow.active_flag == active)
        profile_model = User if uses_canonical_storage() else Author
        profile_id = (
            AuthorFollow.fluxer_author_id
            if uses_canonical_storage()
            else AuthorFollow.author_id
        )
        statement = (
            select(AuthorFollow, profile_model)
            .join(profile_model, profile_model.id == profile_id)
            .where(*conditions)
            .order_by(AuthorFollow.followed_at.desc(), AuthorFollow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        count_statement = (
            select(func.count()).select_from(AuthorFollow).where(*conditions)
        )
        rows = list((await self.session.execute(statement)).all())
        total = int((await self.session.execute(count_statement)).scalar_one())
        return rows, total
