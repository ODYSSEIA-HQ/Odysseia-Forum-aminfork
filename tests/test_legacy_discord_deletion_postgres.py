"""真实 PostgreSQL 验证旧 Discord 删除链路保留业务关系。"""

import os
import pytest
import pytest_asyncio
import test_work_v2_postgres as database_tests

from test_work_v2_postgres import seed_thread
from auditor.auditor_service import AuditorService
from core.thread_deletion_service import ThreadDeletionService
from core.thread_repository import ThreadRepository
from models import Thread, ThreadFollow
from models.work import Work
from models.work_source_state import WorkSourceState

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_WORK_DB_URL"), reason="需要显式隔离 PostgreSQL"
)


@pytest_asyncio.fixture
async def factory(monkeypatch):
    """显式注册独立根事务，兼容原模块同时被收集的组合回归。"""
    async for sessions in database_tests.factory.__wrapped__(monkeypatch):
        yield sessions


@pytest.mark.asyncio
async def test_authoritative_delete_preserves_source_and_follow(factory):
    """真实删除保留来源与关注，主作品和来源墓碑不可恢复。"""
    work_id, row_id = await seed_thread(factory, external=900501)
    async with factory() as session:
        follow = ThreadFollow(
            thread_id=900501, user_id=101, fluxer_user_id="u1", work_id=work_id
        )
        session.add(follow)
        await session.commit()
        follow_id = follow.id
        service = ThreadDeletionService(session)
        assert await service.mark_source_deleted([900501], guild_id=998) == 0
        assert await service.mark_source_deleted([900501], guild_id=999) == 1
        await session.commit()
        row = await session.get(Thread, row_id)
        work = await session.get(Work, work_id)
        state = await session.get(WorkSourceState, ("discord", "900501"))
        assert row.deletion_reason == work.deletion_reason == "source_deleted"
        assert row.show_flag is False
        assert state.deleted
        version = state.version
        assert await session.get(ThreadFollow, follow_id) is not None
        await service.mark_source_deleted([900501], guild_id=999)
        await session.commit()
        await session.refresh(state)
        assert state.version == version
        assert not await ThreadRepository(session).update_thread_visibility(
            900501, True
        )
        await session.refresh(row)
        assert not row.show_flag


@pytest.mark.asyncio
async def test_read_failures_and_platform_collisions_never_delete(factory):
    """读取失败只累计 Discord 计数，不隐藏或误伤同号 Fluxer 来源。"""
    work_id, row_id = await seed_thread(factory, external=900502)
    other_work, other_id = await seed_thread(
        factory, external=900502, platform="fluxer"
    )
    async with factory() as session:
        for _ in range(6):
            assert await ThreadRepository(session).increment_not_found_count(900502)
        assert await AuditorService(session).delete_stale_threads(5) == 0
        row = await session.get(Thread, row_id)
        other = await session.get(Thread, other_id)
        assert row.not_found_count == 6
        assert other.not_found_count == 0
        assert row.show_flag and row.deleted_at is None
        assert (await session.get(Work, work_id)).deleted_at is None
        assert (await session.get(Work, other_work)).deleted_at is None
        batch = await AuditorService(session).get_thread_ids_batch(0, 100000)
        assert (row_id, 900502) in batch
        assert (other_id, 900502) not in batch


@pytest.mark.asyncio
async def test_administrative_removal_is_distinct_and_keeps_relations(factory):
    """管理员移除索引可恢复，并不能覆盖权威来源删除。"""
    work_id, row_id = await seed_thread(factory, external=900503)
    async with factory() as session:
        service = ThreadDeletionService(session)
        assert await service.remove_channel_index(200, thread_id=900503) == 1
        await session.commit()
        row = await session.get(Thread, row_id)
        assert row.deletion_reason == "index_removed"
        assert (await session.get(Work, work_id)).deletion_reason == "index_removed"
        assert await session.get(WorkSourceState, ("discord", "900503")) is None
        await service.mark_channel_source_deleted(200, 999)
        await session.commit()
        await session.refresh(row)
        assert row.deletion_reason == "source_deleted"
        assert await service.remove_channel_index(200, thread_id=900503) == 0


@pytest.mark.asyncio
async def test_child_source_deletion_does_not_delete_work(factory):
    """删除子来源只删除该来源，不软删主作品。"""
    work_id, _ = await seed_thread(factory, external=900504)
    async with factory() as session:
        child = Thread(
            thread_id=900505,
            platform="discord",
            guild_id=999,
            channel_id=200,
            title="子来源",
            work_id=work_id,
            role="child",
        )
        session.add(child)
        await session.commit()
        await ThreadDeletionService(session).mark_source_deleted([900505], guild_id=999)
        await session.commit()
        assert child.deletion_reason == "source_deleted"
        assert (await session.get(Work, work_id)).deleted_at is None
