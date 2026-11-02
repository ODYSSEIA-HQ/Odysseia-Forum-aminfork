"""在独立 PostgreSQL 中验证原生事件隐私、标签、锁来源和创建意图边界。"""

# ruff: noqa: F811
# pytest 跨模块复用显式 fixture，函数参数与导入同名。

import os

import pytest
from sqlalchemy import select, func

from models import Thread
from models.work import Work
from models.work_native_tag_binding import WorkNativeTagBinding
from models.work_native_tag_source import WorkNativeTagSource
from models.work_task import WorkTask
from work.work_event_service import WorkEventService
from test_work_v2_postgres import factory, principal, client, seed_thread  # noqa: F401

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_WORK_DB_URL"), reason="仅运行显式独立测试数据库"
    ),
]
FORUMS = {
    "guild_id": "999",
    "normal": "200",
    "normal_nsfw": "201",
    "abyss": "202",
    "abyss_nsfw": "203",
}


def native_event(version, *, channel="200", **snapshot):
    """构建完整、无凭据的原生事件，版本属于事件投影而非 ThreadState。"""
    return {
        "event_id": f"boundary-native-{version}",
        "platform": "fluxer",
        "guild_id": "999",
        "channel_id": channel,
        "thread_id": "950",
        "state_version": version,
        "snapshot": {
            "title": "原生作品",
            "body": "原生正文",
            "author_id": "u1",
            "images": [],
            "reactions": [],
            "reply_count": 0,
            "locked": False,
            "archived": False,
            "native_locked": False,
            "native_archived": False,
            "external_locked": False,
            **snapshot,
        },
    }


async def test_native_move_immediately_hides_work_and_detail(
    factory, client, principal
):
    """原生移动同步分级并在列表、详情同时拒绝无权读取，旧事件不能降级。"""
    async with factory() as session:
        service = WorkEventService(session, FORUMS)
        assert await service.apply(native_event(1))
        thread = (
            await session.execute(select(Thread).where(Thread.thread_id == 950))
        ).scalar_one()
        work_id = thread.work_id
        await session.commit()
        assert (await client.get(f"/v2/works/{work_id}")).status_code == 200
        assert await service.apply(native_event(3, channel="203"))
        await session.commit()
        work = await session.get(Work, work_id)
        await session.refresh(work)
        assert work.is_nsfw and work.is_abyss and work.source_channel_id == "203"
        principal.update(show_nsfw=False, can_view_abyss=False)
        assert (await client.get(f"/v2/works/{work_id}")).status_code == 404
        result = (await client.get("/v2/works")).json()
        assert result["total"] == 0 and result["items"] == []
        assert await service.apply(native_event(2, channel="200")) is False
        await session.commit()
        await session.refresh(work)
        assert work.is_nsfw and work.is_abyss


async def test_native_tag_add_rename_remove_and_missing_snapshot(factory):
    """原生标签显式快照增删，缺字段不清空，旧版本不能恢复已删来源。"""
    async with factory() as session:
        service = WorkEventService(session, FORUMS)
        assert await service.apply(
            native_event(
                1,
                tags=[
                    {"id": "777", "name": "剧情", "emoji": {"id": None, "name": "📖"}}
                ],
            )
        )
        await session.flush()
        binding = (await session.execute(select(WorkNativeTagBinding))).scalar_one()
        source = await session.get(WorkNativeTagSource, binding.source_id)
        assert source.external_id == "777" and source.name == "剧情"
        assert await service.apply(native_event(2))
        assert (
            await session.execute(
                select(func.count()).select_from(WorkNativeTagBinding)
            )
        ).scalar_one() == 1
        assert await service.apply(
            native_event(3, tags=[{"id": "777", "name": "长篇", "emoji": {}}])
        )
        await session.refresh(source)
        assert source.name == "长篇"
        assert await service.apply(native_event(5, tags=[]))
        assert (
            await session.execute(
                select(func.count()).select_from(WorkNativeTagBinding)
            )
        ).scalar_one() == 0
        assert (
            await service.apply(native_event(4, tags=[{"id": "777", "name": "旧标签"}]))
            is False
        )
        await session.commit()
        assert (
            await session.execute(
                select(func.count()).select_from(WorkNativeTagBinding)
            )
        ).scalar_one() == 0


async def test_external_lock_echo_never_becomes_native_base(factory):
    """有效外部锁只影响展示，不能反写为原生锁或归档并阻碍恢复。"""
    async with factory() as session:
        service = WorkEventService(session, FORUMS)
        assert await service.apply(
            native_event(1, locked=True, archived=True, external_locked=True)
        )
        thread = (
            await session.execute(select(Thread).where(Thread.thread_id == 950))
        ).scalar_one()
        assert (
            thread.archived
            and not thread.platform_locked
            and not thread.native_archived
        )
        assert await service.apply(native_event(2))
        await session.refresh(thread)
        assert (
            not thread.archived
            and not thread.platform_locked
            and not thread.native_archived
        )
        assert await service.apply(
            native_event(
                3, locked=True, archived=True, native_locked=True, native_archived=True
            )
        )
        await session.refresh(thread)
        assert thread.platform_locked and thread.native_archived
        assert (
            await service.apply(native_event(2, locked=False, archived=False)) is False
        )
        await session.commit()
        await session.refresh(thread)
        assert thread.platform_locked and thread.native_archived


async def test_creation_event_before_callback_maps_child_without_duplicate_work(
    factory,
):
    """创建成功事件先于 HTTP 回调到达时按持久任务关联子帖，不误建主作品。"""
    work_id, _ = await seed_thread(factory, external=951, platform="fluxer")
    async with factory() as session:
        task = WorkTask(
            work_id=work_id,
            kind="create_child",
            dedupe_key="boundary-child-intent",
            payload={"actor_id": "u1", "title": "子帖", "body": "内容"},
        )
        session.add(task)
        await session.flush()
        event = {
            **native_event(1),
            "creation_operation_id": f"forum_work_operation_{task.id}",
        }
        service = WorkEventService(session, FORUMS)
        assert await service.apply(event)
        child = (
            await session.execute(select(Thread).where(Thread.thread_id == 950))
        ).scalar_one()
        assert child.role == "child" and child.work_id == work_id
        assert (
            await session.execute(select(func.count()).select_from(Work))
        ).scalar_one() == 1
        assert await service.apply(event) is False
        await session.commit()
