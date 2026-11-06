"""作品子卡区：可见性过滤后复用搜索排序，独立有界分页。"""

from fastapi import HTTPException
from sqlalchemy import select, func, update
from models.thread import Thread
from search.search_service import SearchService
from shared.enum.search_config_type import SearchConfigDefaults
from work.work_service import WorkService
from work.work_moderation_service import WorkModerationService
from work.work_thread_service import WorkThreadService


class WorkCardsService:
    def __init__(self, sessions):
        """保存请求内使用的会话工厂与服务配置。"""
        self.sessions = sessions

    async def page(self, work_id, query, command):
        """在权限过滤后按指定顺序查询并投影分页结果。"""
        principal = command.actor
        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, principal)
            # 仅从当前作品读取已就绪且未删除的子帖。
            filters = [
                Thread.work_id == work_id,
                Thread.role == "child",
                Thread.deleted_at.is_(None),
                Thread.initializing.is_(False),
            ]
            total = (
                await session.execute(
                    select(func.count()).select_from(Thread).where(*filters)
                )
            ).scalar_one()
            statement = select(Thread).where(*filters)
            method = (
                query.custom_base_sort
                if query.sort_method == "custom"
                else query.sort_method
            )
            # 复用搜索排序表达式，并始终用帖子编号稳定处理并列。
            ranking = SearchService(session, None)
            if method == "comprehensive":
                displays = (
                    await session.execute(
                        select(func.coalesce(func.sum(Thread.display_count), 0)).where(
                            *filters
                        )
                    )
                ).scalar_one()
                statement, sort = ranking._apply_ucb1_ranking(
                    statement,
                    displays,
                    float(SearchConfigDefaults.UCB1_EXPLORATION_FACTOR.value),
                    float(SearchConfigDefaults.STRENGTH_WEIGHT.value),
                )
            elif method == "reddit_hot":
                statement, sort = ranking._apply_reddit_hot_ranking(
                    statement, float(SearchConfigDefaults.REDDIT_HOT_TIME_DECAY.value)
                )
            else:
                sort = {
                    "created_at": Thread.created_at,
                    "last_active": Thread.last_active_at,
                    "last_active_at": Thread.last_active_at,
                    "reaction_count": Thread.reaction_count,
                    "reply_count": Thread.reply_count,
                    "collection_count": Thread.collection_count,
                }[method]
            statement = (
                statement.order_by(
                    (
                        sort.asc() if query.sort_order == "asc" else sort.desc()
                    ).nulls_last(),
                    Thread.id.desc(),
                )
                .offset(query.offset)
                .limit(query.limit)
            )
            rows = (await session.execute(statement)).scalars().all()
            items = [WorkThreadService.project(row, principal) for row in rows]
            # 展示内容与创建权限分别判断，禁言不影响已有子卡的读取。
            reason = None
            try:
                await WorkModerationService(session).require_speech(work, principal)
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                reason = exc.detail
            main = (
                (
                    await session.execute(
                        select(Thread).where(
                            Thread.work_id == work_id, Thread.role == "main"
                        )
                    )
                )
                .scalars()
                .first()
            )
            if WorkThreadService.main_unavailable(main):
                reason = "主讨论帖尚未就绪、已锁定或归档，暂不能创建子卡贴"
            forums = principal["forums"]
            channel = forums.get(
                ("abyss" if work.is_abyss else "normal")
                + ("_nsfw" if work.is_nsfw else "")
            )
            if not WorkThreadService.valid_native_id(
                forums.get("guild_id")
            ) or not WorkThreadService.valid_native_id(channel):
                reason = "Fluxer 子帖论坛尚未配置"
            # 一次更新本页全部曝光计数，不逐帖提交。
            if rows and method != "created_at":
                await session.execute(
                    update(Thread)
                    .where(Thread.id.in_([row.id for row in rows]))
                    .values(display_count=func.coalesce(Thread.display_count, 0) + 1)
                )
                await session.commit()
            return {
                "items": items,
                "total": total,
                "offset": query.offset,
                "limit": query.limit,
                "can_create": reason is None,
                "create_disabled_reason": reason,
            }
