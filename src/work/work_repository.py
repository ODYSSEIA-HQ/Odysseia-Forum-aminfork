"""作品筛选、全文检索与分页仓储。"""

from types import SimpleNamespace
from sqlalchemy import select, func, or_, and_, not_, case
from models.work import Work
from models.work_statistics import WorkStatistics
from models.booklist_item import BooklistItem
from models.booklist import Booklist
from models.user import User
from shared.fts_utils import build_fts_conditions
from shared.keyword_parser import parse_search_keywords
from shared.time_parser import parse_time_string
from core.tag_query import tag_filters, tag_name_filters, require_current_tag_ids
from search.search_service import SearchService
from shared.enum.search_config_type import SearchConfigDefaults
from shared.redis_client import RedisManager


class WorkRepository:
    """分页数据与计数共享权限、FTS、标签与范围过滤。"""

    def __init__(self, session):
        """复用调用方会话和既有搜索算法。"""
        self.session = session

    async def page(
        self,
        principal,
        offset,
        limit,
        q=None,
        is_nsfw=None,
        is_abyss=None,
        author_id=None,
        tag_id=None,
        work_ids=None,
        advanced=None,
        deleted_only=False,
        channel_platform=None,
    ):
        """先执行既有 grouped AND/OR 搜索及访问过滤，再排序分页。"""
        abyss_config = principal.get("config", {}).get("abyss")
        abyss_channels = {
            str(value) for value in (abyss_config or {}).get("channel_ids", [])
        }
        abyss_column = (
            case(
                (
                    Work.source_platform == "discord",
                    Work.source_channel_id.in_(abyss_channels),
                ),
                else_=Work.is_abyss,
            )
            if abyss_config is not None
            else Work.is_abyss
        )
        filters = [
            Work.deleted_at.is_not(None) if deleted_only else Work.deleted_at.is_(None)
        ]
        if not deleted_only:
            filters.append(Work.source_visible.is_(True))
        if not principal["show_nsfw"]:
            filters.append(Work.is_nsfw.is_(False))
        if not principal["can_view_abyss"]:
            filters.append(abyss_column.is_(False))
            abyss_channels = [
                str(value)
                for value in principal.get("config", {})
                .get("abyss", {})
                .get("channel_ids", [])
            ]
            if abyss_channels:
                filters.append(
                    not_(
                        and_(
                            Work.source_platform == "discord",
                            Work.source_channel_id.in_(abyss_channels),
                        )
                    )
                )
        if is_nsfw is not None:
            filters.append(Work.is_nsfw == is_nsfw)
        if is_abyss is not None:
            filters.append(abyss_column == is_abyss)
        if author_id is not None:
            filters.append(Work.author_id == author_id)
        if tag_id is not None:
            filters.extend(tag_filters("work", Work.id, [tag_id], []))
        if work_ids is not None:
            filters.append(Work.id.in_(work_ids))
        options = advanced
        keywords = options.keywords if options else q
        excluded = options.exclude_keywords if options else None
        author_name, keywords, excluded = parse_search_keywords(keywords, excluded)
        markers = options.exclude_keyword_exemption_markers if options else None
        fts = await build_fts_conditions(
            Work.search_vector,
            keywords,
            excluded,
            markers,
            redis_client=RedisManager._client,
        )
        filters.extend(fts.include_conditions)
        if fts.has_exclude:
            filters.append(~fts.exclude_condition)
        base = select(Work).outerjoin(WorkStatistics, WorkStatistics.work_id == Work.id)
        ranking = SearchService(self.session, None)
        if options:
            filters.extend(
                await self.advanced_filters(
                    principal, options, author_name, channel_platform=channel_platform
                )
            )
            ranking._apply_range_filter(
                filters,
                func.coalesce(WorkStatistics.reaction_count, 0),
                options.reaction_count_range,
            )
            ranking._apply_range_filter(
                filters,
                func.coalesce(WorkStatistics.reply_count, 0),
                options.reply_count_range,
            )
        elif author_name:
            filters.append(
                Work.author_id.in_(
                    select(User.id).where(
                        or_(
                            User.username.ilike("%" + author_name + "%"),
                            User.display_name.ilike("%" + author_name + "%"),
                        )
                    )
                )
            )
        total = (
            await self.session.execute(
                select(func.count()).select_from(base.where(*filters).subquery())
            )
        ).scalar_one()
        statement = base.where(*filters)
        method = (
            (
                options.custom_base_sort
                if options.sort_method == "custom"
                else options.sort_method
            )
            if options
            else "created_at"
        )
        descending = not options or options.sort_order != "asc"
        projection = SimpleNamespace(
            reaction_count=func.coalesce(WorkStatistics.reaction_count, 0),
            display_count=func.coalesce(WorkStatistics.display_count, 0),
            created_at=Work.created_at,
        )
        if method == "comprehensive":
            displays = (
                await self.session.execute(
                    select(func.coalesce(func.sum(WorkStatistics.display_count), 0))
                )
            ).scalar_one()
            statement, sort = ranking._apply_ucb1_ranking(
                statement,
                displays,
                float(SearchConfigDefaults.UCB1_EXPLORATION_FACTOR.value),
                float(SearchConfigDefaults.STRENGTH_WEIGHT.value),
                model=projection,
            )
        elif method == "reddit_hot":
            statement, sort = ranking._apply_reddit_hot_ranking(
                statement,
                float(SearchConfigDefaults.REDDIT_HOT_TIME_DECAY.value),
                model=projection,
            )
        elif method == "collected_at" and options.search_by_collection:
            sort = (
                select(func.max(BooklistItem.created_at))
                .where(
                    BooklistItem.work_id == Work.id,
                    BooklistItem.fluxer_owner_id == principal["id"],
                )
                .scalar_subquery()
            )
        else:
            sortable = {
                "created_at": Work.created_at,
                "last_active": Work.last_active_at,
                "last_active_at": Work.last_active_at,
                "reaction_count": func.coalesce(WorkStatistics.reaction_count, 0),
                "reply_count": func.coalesce(WorkStatistics.reply_count, 0),
                "collection_count": func.coalesce(WorkStatistics.collection_count, 0),
                "view_count": func.coalesce(WorkStatistics.view_count, 0),
            }
            sort = sortable.get(method, Work.last_active_at)
        statement = (
            statement.order_by(
                (sort.desc() if descending else sort.asc()).nulls_last(), Work.id.desc()
            )
            .offset(offset)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).scalars().all()
        if abyss_config is not None:
            for work in rows:
                if work.source_platform == "discord":
                    work.is_abyss = work.source_channel_id in abyss_channels
        return rows, total

    async def advanced_filters(
        self, principal, options, author_name, *, channel_platform=None
    ):
        """复用标签、时间和作者语义，不把隐藏频道放入结果后再过滤。"""
        mappings = {
            int(key): [
                {
                    **item,
                    "source_channel_ids": [
                        int(value) for value in item.get("source_channel_ids", [])
                    ],
                }
                for item in entries
            ]
            for key, entries in principal.get("config", {})
            .get("channel_mappings", {})
            .items()
            if str(key).isdecimal() and isinstance(entries, list)
        }
        include_tags, exclude_tags = options.include_tags, options.exclude_tags
        channel_ids = options.channel_ids or None
        selected_channels = bool(options.channel_ids)
        if channel_platform is None and (
            selected_channels or options.exclude_channel_ids
        ):
            channel_platform = options.platform or "discord"
        # 既有虚拟频道/标签映射来自 Discord，不能解释原生同数字 ID。
        if (
            channel_platform == "fluxer"
            or options.platform == "fluxer"
            and not selected_channels
        ):
            mappings = {}
        virtual_names = {
            entry["tag_name"] for entries in mappings.values() for entry in entries
        }
        included_virtual = bool(virtual_names.intersection(include_tags))
        excluded_virtual = bool(virtual_names.intersection(exclude_tags))
        if included_virtual:
            channel_platform = "discord"
        if mappings:
            from shared.channel_mapping_utils import ChannelMappingUtils

            indexed = (
                (
                    await self.session.execute(
                        select(Work.source_channel_id)
                        .where(Work.source_platform == "discord")
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            resolution = ChannelMappingUtils(mappings).resolve(
                [int(value) for value in channel_ids] if channel_ids else None,
                include_tags,
                exclude_tags,
                options.tag_logic,
                [int(value) for value in indexed if str(value).isdecimal()],
            )
            channel_ids = resolution.effective_channel_ids
            include_tags = resolution.effective_include_tags
            exclude_tags = resolution.effective_exclude_tags
        filters = []
        if options.platform:
            filters.append(Work.source_platform == options.platform)
        if options.guild_id:
            filters.append(Work.source_guild_id == str(options.guild_id))
        if selected_channels or included_virtual:
            filters.append(Work.source_platform == (channel_platform or "discord"))
        if channel_ids is not None:
            condition = Work.source_channel_id.in_(
                [str(value) for value in channel_ids]
            )
            # 反选旧虚拟频道只限制 Discord 来源，不清空不属于该命名空间的原生作品。
            if excluded_virtual and not selected_channels and not included_virtual:
                condition = or_(Work.source_platform != "discord", condition)
            filters.append(condition)
        if options.exclude_channel_ids:
            filters.append(
                or_(
                    Work.source_platform
                    != (channel_platform or options.platform or "discord"),
                    Work.source_channel_id.not_in(
                        [str(value) for value in options.exclude_channel_ids]
                    ),
                )
            )
        if options.exclude_work_ids:
            filters.append(
                Work.id.not_in([int(value) for value in options.exclude_work_ids])
            )
        if options.exclude_thread_ids:
            filters.append(
                Work.source_thread_id.not_in(
                    [str(value) for value in options.exclude_thread_ids]
                )
            )
        if options.include_authors:
            filters.append(
                Work.author_id.in_([str(value) for value in options.include_authors])
            )
        if options.exclude_authors:
            filters.append(
                or_(
                    Work.author_id.is_(None),
                    Work.author_id.not_in(
                        [str(value) for value in options.exclude_authors]
                    ),
                )
            )
        name = author_name or options.author_name
        if name:
            filters.append(
                Work.author_id.in_(
                    select(User.id).where(
                        or_(
                            User.username.ilike("%" + name + "%"),
                            User.display_name.ilike("%" + name + "%"),
                        )
                    )
                )
            )
        await require_current_tag_ids(
            self.session, [*options.include_tag_ids, *options.exclude_tag_ids]
        )
        filters.extend(
            tag_filters(
                "work",
                Work.id,
                options.include_tag_ids,
                options.exclude_tag_ids,
                options.tag_logic,
            )
        )
        filters.extend(
            await tag_name_filters(
                self.session,
                "work",
                Work.id,
                include_tags,
                exclude_tags,
                options.tag_logic,
            )
        )
        for field, column, operator in (
            ("created_after", Work.created_at, "after"),
            ("created_before", Work.created_at, "before"),
            ("active_after", Work.last_active_at, "after"),
            ("active_before", Work.last_active_at, "before"),
        ):
            value = getattr(options, field)
            if value:
                parsed = parse_time_string(value)
                filters.append(
                    column >= parsed if operator == "after" else column <= parsed
                )
        if options.search_by_collection:
            filters.append(
                Work.id.in_(
                    select(BooklistItem.work_id)
                    .join(Booklist, Booklist.id == BooklistItem.booklist_id)
                    .where(
                        Booklist.fluxer_owner_id == principal["id"],
                        Booklist.is_default.is_(True),
                    )
                )
            )
        return filters
