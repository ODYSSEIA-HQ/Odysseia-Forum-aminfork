"""初始化开放成功后的投影、事务和安全边界回归。"""

# ruff: noqa: F811
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from models.thread import Thread
from models.work_task import WorkTask
from work.work_task_worker import WorkTaskWorker
from datetime import timedelta
from models.work import Work
from shared.time_utils import utc_now
from test_work_v2_postgres import factory, principal, client, pytestmark  # noqa: F401


async def seed(factory):
    """准备主帖、可见子帖、初始化子帖和删除子帖的独立样本。"""
    async with factory() as session:
        work = Work(
            title="作品",
            author_id="u1",
            source_platform="discord",
            source_thread_id="99901",
        )
        other = Work(title="其他作品", author_id="u1")
        session.add_all([work, other])
        await session.flush()
        rows = []
        for i, role in enumerate(["main", "child", "child", "child", "child", "child"]):
            row = Thread(
                work_id=other.id if i == 5 else work.id,
                platform="discord" if i == 0 else "fluxer",
                thread_id=9007199254740993100 + i,
                guild_id=999,
                channel_id=200,
                title=f"卡{i}",
                body="正文",
                role=role,
                fluxer_author_id="u1",
                created_at=utc_now() - timedelta(days=i),
                reaction_count=10 * i,
                reply_count=6 - i,
                display_count=i + 1,
                collection_count=i,
                deleted_at=utc_now() if i == 3 else None,
                initializing=i == 4,
            )
            session.add(row)
            rows.append(row)
        await session.flush()
        ids = [row.id for row in rows]
        # 完成由建档触发的无关投影任务，确保测试领取目标开放任务。
        await session.execute(
            update(WorkTask)
            .where(WorkTask.kind == "projection", WorkTask.status == "pending")
            .values(status="done")
        )
        await session.commit()
        return work.id, ids


async def release_task(factory, wid, row_id, **changes):
    """准备仅作用于指定子帖的开放任务。"""
    async with factory() as session:
        child = await session.get(Thread, row_id)
        payload = {
            "actor_id": "u1",
            "guild_id": str(child.guild_id),
            "thread_id": str(child.thread_id),
            "source": "forum_initializing",
            "active": False,
            "depends_on": [],
        }
        payload.update(changes)
        task = WorkTask(
            work_id=wid,
            kind="policy",
            dedupe_key=f"release-child:{row_id}",
            payload=payload,
        )
        session.add(task)
        await session.commit()
        return task.id


@pytest.mark.asyncio
async def test_success_replay_and_all_card_sorts(factory, client):
    """成功及重放只清初始化，不清其他锁；全部排序均显示已开放子卡。"""
    wid, ids = await seed(factory)
    task_id = await release_task(factory, wid, ids[4])
    async with factory() as session:
        child = await session.get(Thread, ids[4])
        child.lock_owner_ids = ["u2"]
        child.platform_locked = True
        await session.commit()
    rpc = AsyncMock(return_value={})
    worker = WorkTaskWorker(factory, rpc=rpc)
    assert await worker.once()
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        child = await session.get(Thread, ids[4])
        assert task.status == "done" and not child.initializing
        assert child.lock_owner_ids == ["u2"] and child.platform_locked
        await worker.apply_result(session, task, {})
        await session.commit()
    for method in (
        "last_active",
        "created_at",
        "reply_count",
        "reaction_count",
        "comprehensive",
        "reddit_hot",
        "collection_count",
    ):
        for order in ("asc", "desc"):
            response = await client.get(
                f"/v2/works/{wid}/cards",
                params={"sort_method": method, "sort_order": order},
            )
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 3
            assert str(ids[4]) in {row["id"] for row in response.json()["items"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [{"source": "forum_soft_delete"}, {"active": True}, {"target_user_id": "102"}],
)
async def test_other_policies_cannot_open(factory, changes):
    """其他策略、重新加锁、用户级策略均不能开放子帖。"""
    wid, ids = await seed(factory)
    task_id = await release_task(factory, wid, ids[4], **changes)
    assert await WorkTaskWorker(factory, rpc=AsyncMock(return_value={})).once()
    async with factory() as session:
        assert (await session.get(WorkTask, task_id)).status == "done"
        assert (await session.get(Thread, ids[4])).initializing


@pytest.mark.asyncio
async def test_rpc_failure_stays_hidden(factory, client):
    """原生操作失败时不能提前开放。"""
    wid, ids = await seed(factory)
    task_id = await release_task(factory, wid, ids[4])
    assert await WorkTaskWorker(
        factory, rpc=AsyncMock(side_effect=RuntimeError("offline"))
    ).once()
    async with factory() as session:
        assert (await session.get(WorkTask, task_id)).status == "pending"
        assert (await session.get(Thread, ids[4])).initializing
    assert (await client.get(f"/v2/works/{wid}/cards")).json()["total"] == 2


@pytest.mark.asyncio
async def test_pending_dependency_prevents_rpc(factory):
    """前置处罚未完成时不调用开放 RPC。"""
    wid, ids = await seed(factory)
    task_id = await release_task(factory, wid, ids[4], depends_on=[999999999])
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        # 使用真实存在但尚未完成的任务作为依赖。
        task.payload = {**task.payload, "depends_on": [task_id]}
        await session.commit()
    rpc = AsyncMock(return_value={})
    await WorkTaskWorker(factory, rpc=rpc).once()
    rpc.assert_not_awaited()
    async with factory() as session:
        assert (await session.get(Thread, ids[4])).initializing


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["guild", "work", "key", "main"])
async def test_mismatched_target_fails_closed(factory, mismatch):
    """社区、作品、去重键或角色不匹配时保持关闭。"""
    wid, ids = await seed(factory)
    task_id = await release_task(factory, wid, ids[4])
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        if mismatch == "guild":
            task.payload = {**task.payload, "guild_id": "998"}
        elif mismatch == "work":
            task.work_id = wid + 9999
        elif mismatch == "key":
            task.dedupe_key = "unrelated"
        else:
            main = await session.get(Thread, ids[0])
            main.platform = "fluxer"
            main.initializing = True
            task.payload = {**task.payload, "thread_id": "9007199254740993100"}
        await session.commit()
    await WorkTaskWorker(factory, rpc=AsyncMock(return_value={})).once()
    async with factory() as session:
        assert (await session.get(WorkTask, task_id)).status == "pending"
        assert (await session.get(Thread, ids[4])).initializing


@pytest.mark.asyncio
async def test_rollback_and_deleted_child_stay_safe(factory, client):
    """回滚不泄漏开放状态，已删除子帖开放后仍不显示。"""
    wid, ids = await seed(factory)
    task_id = await release_task(factory, wid, ids[4])
    worker = WorkTaskWorker(factory, rpc=AsyncMock(return_value={}))
    async with factory() as session:
        await worker.apply_result(session, await session.get(WorkTask, task_id), {})
        await session.rollback()
        assert (await session.get(Thread, ids[4])).initializing
        from shared.time_utils import utc_now

        child = await session.get(Thread, ids[4])
        child.deleted_at = utc_now()
        child.deletion_reason = "source_deleted"
        await session.commit()
    await worker.once()
    assert (await client.get(f"/v2/works/{wid}/cards")).json()["total"] == 2


@pytest.mark.asyncio
async def test_stale_lease_cannot_publish_release(factory):
    """旧执行者返回成功也不能覆盖较新的租约。"""
    wid, ids = await seed(factory)
    task_id = await release_task(factory, wid, ids[4])

    async def rpc(*args, **kwargs):
        async with factory() as session:
            task = await session.get(WorkTask, task_id)
            task.attempts += 1
            await session.commit()
        return {}

    await WorkTaskWorker(factory, rpc=rpc).once()
    async with factory() as session:
        assert (await session.get(WorkTask, task_id)).status == "running"
        assert (await session.get(Thread, ids[4])).initializing
