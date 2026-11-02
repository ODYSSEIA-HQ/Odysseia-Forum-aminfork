from identity.runtime import is_fluxer_mode
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import case, select

from core.thread_repository import ThreadRepository
from dto.preferences.user_search_preferences_dto import UserSearchPreferencesDTO
from models import Tag, Thread


class DiscoveryRepository:
    """提供发现页所需的底层数据拉取功能"""

    def __init__(
        self,
        session: AsyncSession,
        exclude_channel_ids: list[int] | None = None,
    ):
        self.session = session
        self.thread_repo = ThreadRepository(session)
        self.exclude_channel_ids = exclude_channel_ids or []
        # 仓储随请求创建，只缓存偏好条件，不缓存轨道范围、排序或分页。
        self._preference_conditions: dict[str, tuple] = {}

    async def _apply_preferences_filter(
        self, stmt, prefs: UserSearchPreferencesDTO | None
    ):
        """应用用户偏好到查询构建器"""
        if not prefs:
            return stmt

        # 固定本次解析的值，避免调用方原地修改 DTO 后误用旧条件。
        prefs = prefs.model_copy(deep=True)
        cache_key = prefs.model_dump_json()
        if cache_key in self._preference_conditions:
            return stmt.where(*self._preference_conditions[cache_key])
        conditions = []

        # 频道过滤
        if prefs.preferred_channels:
            conditions.append(Thread.channel_id.in_(prefs.preferred_channels))  # type: ignore

        # 作者过滤
        if prefs.exclude_authors:
            conditions.append(
                (
                    Thread.fluxer_author_id if is_fluxer_mode() else Thread.author_id
                ).notin_(prefs.exclude_authors)
            )  # type: ignore

        # 标签过滤 (先查出 ID 再用 EXISTS 进行 OR 匹配)
        if prefs.include_tags or prefs.exclude_tags:
            all_tags = []
            if prefs.include_tags:
                all_tags.extend(prefs.include_tags)
            if prefs.exclude_tags:
                all_tags.extend(prefs.exclude_tags)

            tag_stmt = select(Tag.id, Tag.name).where(Tag.name.in_(all_tags))  # type: ignore
            tag_res = await self.session.execute(tag_stmt)
            tag_map = {name: tid for tid, name in tag_res.fetchall()}

            include_tag_ids = (
                [tag_map[name] for name in prefs.include_tags if name in tag_map]
                if prefs.include_tags
                else []
            )
            exclude_tag_ids = (
                [tag_map[name] for name in prefs.exclude_tags if name in tag_map]
                if prefs.exclude_tags
                else []
            )

            if include_tag_ids:
                # 默认 or 逻辑，只要带有一个偏好标签就放行
                conditions.append(Thread.tags.any(Tag.id.in_(include_tag_ids)))  # type: ignore
            if exclude_tag_ids:
                conditions.append(~Thread.tags.any(Tag.id.in_(exclude_tag_ids)))  # type: ignore

        # FTS5 正反选关键词过滤处理
        fts_result = await self.thread_repo.get_fts_matched_thread_ids(
            keywords=prefs.include_keywords,
            exclude_keywords=prefs.exclude_keywords,
            exemption_markers=prefs.exclude_keyword_exemption_markers,
        )

        if fts_result.has_include:
            conditions.extend(fts_result.include_conditions)
        if fts_result.has_exclude:
            conditions.append(~fts_result.exclude_condition)

        # 解析全部成功后发布缓存，失败重试不会使用部分构建的条件。
        self._preference_conditions[cache_key] = tuple(conditions)
        return stmt.where(*conditions)

    async def get_latest_threads(
        self,
        limit: int,
        offset: int,
        prefs: UserSearchPreferencesDTO | None,
        channel_ids: list[int] | None = None,
    ) -> list[Thread]:
        """拉取最新的帖子列表"""
        stmt = select(Thread).where(
            Thread.deleted_at.is_(None), Thread.show_flag.is_(True)
        )  # type: ignore

        # 显式频道范围为空时必须返回空结果，不能退化为全频道查询。
        if channel_ids is not None:
            stmt = stmt.where(Thread.channel_id.in_(channel_ids))  # type: ignore

        # 过滤广场推荐忽略频道
        if self.exclude_channel_ids:
            stmt = stmt.where(Thread.channel_id.notin_(self.exclude_channel_ids))  # type: ignore

        # 应用偏好过滤器并 await
        stmt = await self._apply_preferences_filter(stmt, prefs)

        stmt = stmt.options(selectinload(Thread.tags), joinedload(Thread.author))  # type: ignore
        stmt = stmt.order_by(Thread.created_at.desc()).offset(offset).limit(limit)  # type: ignore

        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_threads_by_ids_ordered(
        self,
        thread_ids: list[int],
        prefs: UserSearchPreferencesDTO | None,
        channel_ids: list[int] | None = None,
    ) -> list[Thread]:
        """拉取指定ID列表的帖子并依靠传入的列表保持顺序不变"""
        if not thread_ids:
            return []

        stmt = select(Thread).where(
            Thread.thread_id.in_(thread_ids),  # type: ignore
            Thread.deleted_at.is_(None),
            Thread.show_flag.is_(True),  # type: ignore
        )

        # 对迁移完成前的全局趋势回退和最终数据校验统一应用频道范围。
        if channel_ids is not None:
            stmt = stmt.where(Thread.channel_id.in_(channel_ids))  # type: ignore

        # 过滤广场推荐忽略频道
        if self.exclude_channel_ids:
            stmt = stmt.where(Thread.channel_id.notin_(self.exclude_channel_ids))  # type: ignore

        # 应用偏好过滤器并 await
        stmt = await self._apply_preferences_filter(stmt, prefs)

        stmt = stmt.options(selectinload(Thread.tags), joinedload(Thread.author))  # type: ignore

        order_case = case(
            {tid: index for index, tid in enumerate(thread_ids)}, value=Thread.thread_id
        )
        stmt = stmt.order_by(order_case)

        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())
