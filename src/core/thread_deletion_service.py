"""保留业务关系的 Discord 来源删除与行政移除索引。"""

import logging

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Thread
from models.work import Work
from models.work_source_state import WorkSourceState
from shared.time_utils import utc_now

logger = logging.getLogger(__name__)


class ThreadDeletionService:
    """只接受明确来源事实，不从读取失败推断删除。"""

    def __init__(self, session: AsyncSession):
        """复用调用方事务，确保来源和作品投影一起提交。"""
        self.session = session

    async def delete_thread(self, thread_id: int) -> int:
        """兼容旧权威删除入口，保留帖子及全部关联。"""
        return await self.mark_source_deleted([thread_id])

    async def delete_threads(self, thread_ids: list[int]) -> int:
        """兼容旧权威批量入口，禁止物理清理关联。"""
        return await self.mark_source_deleted(thread_ids)

    async def delete_channel(self, channel_id: int) -> int:
        """兼容管理员移除索引入口，不声称 Discord 内容已删除。"""
        return await self.remove_channel_index(channel_id)

    async def delete_stale(self, threshold: int) -> int:
        """连续读取失败只保留计数和重试，不改变作品可见性。"""
        logger.info("保留读取失败次数达到 %s 的来源，等待后续同步", threshold)
        return 0

    async def remove_channel_index(
        self, channel_id: int, *, thread_id: int | None = None
    ) -> int:
        """行政移除 Discord 频道索引，可恢复且不删除关系。"""
        statement = (
            select(Thread)
            .where(Thread.platform == "discord", Thread.channel_id == channel_id)
            .execution_options(all_platforms=True)
        )
        if thread_id is not None:
            statement = statement.where(Thread.thread_id == thread_id)
        candidates = list((await self.session.execute(statement)).scalars().all())
        for work_id in sorted(
            {row.work_id for row in candidates if row.work_id is not None}
        ):
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:id)"), {"id": work_id}
            )
        rows = list(
            (
                await self.session.execute(
                    statement.order_by(Thread.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        now = utc_now()
        work_ids = set()
        changed = 0
        for thread in rows:
            if (
                thread.deleted_at is not None
                and thread.deletion_reason != "index_removed"
            ):
                continue
            thread.show_flag = False
            thread.deleted_at = thread.deleted_at or now
            thread.deletion_reason = "index_removed"
            if thread.work_id is not None and thread.role == "main":
                work_ids.add(thread.work_id)
            changed += 1
        if work_ids:
            await self.session.execute(
                update(Work)
                .where(
                    Work.id.in_(work_ids),
                    Work.deletion_reason.is_distinct_from("source_deleted"),
                )
                .values(deleted_at=now, deletion_reason="index_removed")
            )
        await self.session.flush()
        return changed

    async def mark_channel_source_deleted(self, channel_id: int, guild_id: int) -> int:
        """仅供真实 guild channel 删除事件标记整个 Discord 来源频道。"""
        statement = (
            select(Thread.thread_id)
            .where(
                Thread.platform == "discord",
                Thread.channel_id == channel_id,
                Thread.guild_id == guild_id,
            )
            .execution_options(all_platforms=True)
        )
        ids = list((await self.session.execute(statement)).scalars().all())
        return await self.mark_source_deleted(ids, guild_id=guild_id)

    async def mark_source_deleted(
        self, thread_ids: list[int], *, guild_id: int | None = None
    ) -> int:
        """权威 gateway 删除写墓碑、隐藏来源及主作品，永久保留关系。"""
        ids = sorted(set(thread_ids))
        if not ids:
            return 0
        # 与内容事件使用相同来源锁，禁止并发快照越过删除墓碑。
        for external_id in ids:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"source:discord:{external_id}"},
            )
        statement = select(Thread).where(
            Thread.platform == "discord", Thread.thread_id.in_(ids)
        )
        if guild_id is not None:
            statement = statement.where(Thread.guild_id == guild_id)
        candidates = list(
            (
                await self.session.execute(
                    statement.execution_options(all_platforms=True)
                )
            )
            .scalars()
            .all()
        )
        work_ids = sorted(
            {row.work_id for row in candidates if row.work_id is not None}
        )
        for work_id in work_ids:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:id)"), {"id": work_id}
            )
        rows = list(
            (
                await self.session.execute(
                    statement.order_by(Thread.id)
                    .with_for_update()
                    .execution_options(all_platforms=True, populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        now = utc_now()
        linked_ids = [str(row.thread_id) for row in rows if row.work_id is not None]
        states = {}
        if linked_ids:
            state_rows = (
                (
                    await self.session.execute(
                        select(WorkSourceState)
                        .where(
                            WorkSourceState.platform == "discord",
                            WorkSourceState.thread_id.in_(linked_ids),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            states = {state.thread_id: state for state in state_rows}
        for thread in rows:
            if thread.work_id is not None:
                state = states.get(str(thread.thread_id))
                if state is None:
                    state = WorkSourceState(
                        platform="discord",
                        thread_id=str(thread.thread_id),
                        version=thread.source_version + 1,
                        deleted=True,
                    )
                    self.session.add(state)
                elif not state.deleted:
                    state.version = max(state.version, thread.source_version) + 1
                    state.deleted = True
                thread.source_version = max(thread.source_version, state.version)
            thread.deleted_at = thread.deleted_at or now
            thread.deletion_reason = "source_deleted"
            thread.show_flag = False
        main_work_ids = {
            row.work_id
            for row in rows
            if row.role == "main" and row.work_id is not None
        }
        if main_work_ids:
            await self.session.execute(
                update(Work)
                .where(Work.id.in_(main_work_ids))
                .values(deleted_at=now, deletion_reason="source_deleted")
            )
        await self.session.flush()
        return len(rows)
