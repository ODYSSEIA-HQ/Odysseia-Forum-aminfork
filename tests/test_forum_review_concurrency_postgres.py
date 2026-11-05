"""显式一次性 PostgreSQL 中验证编辑顺序和分类创建互斥，不连接真实 NATS。"""

import asyncio
import os
import re
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from identity.integration_rpc_error import IntegrationRpcError
from models import DiscordIdentity, Thread, User
from models.work import Work
from models.work_task import WorkTask
from shared.time_utils import utc_now
from work.work_service import WorkService
from work.work_task_worker import WorkTaskWorker

URL = os.environ.get("FORUM_REVIEW_TEST_DB_URL")
ACK = os.environ.get("FORUM_REVIEW_TEST_DB_ACK")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL or ACK != "isolated-disposable", reason="需明确一次性 FORUM_REVIEW_TEST_DB_URL 及隔离确认"),
]


@pytest_asyncio.fixture
async def isolated_review_factory():
    """只在已确认的回环测试库创建随机 schema，多连接提交可见且最终完整清理。"""
    url = make_url(URL)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.port is None
        or not re.fullmatch(r"odysseia_review_[a-z0-9_]+", url.database or "")
    ):
        pytest.fail("隔离测试库必须为显式回环端口及 odysseia_review_ 前缀，未连接数据库")
    from identity.runtime import request_identity_mode

    marker = request_identity_mode.set("fluxer")
    schema = "test_forum_review_" + uuid.uuid4().hex
    engine = create_async_engine(url, pool_size=6, max_overflow=0, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(SQLModel.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield factory
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            try:
                await engine.dispose()
            finally:
                request_identity_mode.reset(marker)


async def seed_work(factory):
    """建立最小讨论区与作品，固定测试数据不触及已有 schema 或真实账号。"""
    async with factory() as session:
        work = Work(title="review work", author_id="100", source_platform="discord", source_channel_id="20", source_guild_id="9")
        session.add(work)
        await session.flush()
        thread = Thread(platform="fluxer", thread_id=800, guild_id=9, channel_id=20, work_id=work.id, role="main", title="review thread", body="initial", author_id=None, fluxer_author_id="100")
        session.add(thread)
        await session.commit()
        return work.id


async def seed_edits(factory, work_id):
    """独立幂等编号的旧新编辑共享同一原生帖子。"""
    async with factory() as session:
        tasks = [WorkTask(work_id=work_id, kind="edit_thread", dedupe_key="review-edit-" + label, payload={"actor_id": "100", "guild_id": "9", "thread_id": "800", "body": label}) for label in ("old", "new")]
        session.add_all(tasks)
        await session.commit()
        return [task.id for task in tasks]


async def native_body(factory, value):
    """网络替身只写当前测试 schema 的模拟原生正文。"""
    async with factory() as session:
        await session.execute(update(Thread).where(Thread.platform == "fluxer", Thread.thread_id == 800).values(body=value))
        await session.commit()


async def assert_newest_body(factory):
    """通过另一数据库连接核实最后正文与已提交的任务状态。"""
    async with factory() as session:
        assert (await session.execute(select(Thread.body).where(Thread.thread_id == 800))).scalar_one() == "new"
        statuses = (await session.execute(select(WorkTask.status).where(WorkTask.kind == "edit_thread").order_by(WorkTask.id))).scalars().all()
        assert statuses[-1] == "done"


async def test_two_workers_cannot_overtake_running_edit(isolated_review_factory):
    """另一连接即使 SKIP LOCKED 也不能越过已提交 running 的旧编辑。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    await seed_edits(factory, work_id)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def rpc(subject, **fields):
        calls.append(fields["content"])
        if fields["content"] == "old":
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
        await native_body(factory, fields["content"])
        return {}
    first = asyncio.create_task(WorkTaskWorker(factory, rpc).once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not await WorkTaskWorker(factory, rpc).once()
        assert calls == ["old"]
    finally:
        release.set()
        await asyncio.wait_for(first, 5)
    assert await WorkTaskWorker(factory, rpc).once()
    assert calls == ["old", "new"]
    await assert_newest_body(factory)


async def test_retry_backoff_blocks_newer_until_old_resolves(isolated_review_factory):
    """旧编辑尚在 backoff 时新编辑仍被数据库谓词挡住。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    old_id, _ = await seed_edits(factory, work_id)
    calls = []
    async def rpc(subject, **fields):
        calls.append(fields["content"])
        if len(calls) == 1:
            raise RuntimeError("fake transport failed before application")
        await native_body(factory, fields["content"])
        return {}
    worker = WorkTaskWorker(factory, rpc)
    assert await worker.once()
    assert not await WorkTaskWorker(factory, rpc).once()
    assert calls == ["old"]
    async with factory() as session:
        await session.execute(update(WorkTask).where(WorkTask.id == old_id).values(available_at=utc_now() - timedelta(seconds=1)))
        await session.commit()
    assert await worker.once()
    assert await worker.once()
    assert calls == ["old", "old", "new"]
    await assert_newest_body(factory)


async def test_permanent_failure_releases_following_edit(isolated_review_factory):
    """不可重试的确定失败保持 failed，不无限阻塞较新的编辑。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    old_id, _ = await seed_edits(factory, work_id)
    async def rpc(subject, **fields):
        if fields["content"] == "old":
            raise IntegrationRpcError("REVIEW_DEFINITE_DENIAL", False)
        await native_body(factory, fields["content"])
        return {}
    worker = WorkTaskWorker(factory, rpc)
    assert await worker.once()
    assert await worker.once()
    async with factory() as session:
        assert (await session.get(WorkTask, old_id)).status == "failed"
    await assert_newest_body(factory)


async def test_worker_cancel_reclaims_same_operation_before_new_edit(isolated_review_factory):
    """取消执行进程不等于取消业务，保留 running 租约并以原幂等编号接续。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    old_id, _ = await seed_edits(factory, work_id)
    entered = asyncio.Event()
    calls = []
    async def rpc(subject, **fields):
        calls.append((fields["operation_id"], fields["content"]))
        if len(calls) == 1:
            entered.set()
            await asyncio.Event().wait()
        await native_body(factory, fields["content"])
        return {}
    first = asyncio.create_task(WorkTaskWorker(factory, rpc).once())
    await asyncio.wait_for(entered.wait(), 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    worker = WorkTaskWorker(factory, rpc)
    assert not await worker.once()
    async with factory() as session:
        task = await session.get(WorkTask, old_id)
        assert task.status == "running" and task.dispatched_at is not None
        task.lease_until = utc_now() - timedelta(seconds=1)
        await session.commit()
    assert await worker.once()
    assert await worker.once()
    assert calls[0][0] == calls[1][0] != calls[2][0]
    assert [value for _, value in calls] == ["old", "old", "new"]
    await assert_newest_body(factory)


@pytest.mark.parametrize("status,marked,blocked", [("running", True, True), ("pending", True, True), ("failed", True, True), ("failed", False, False), ("done", True, False)])
async def test_classification_respects_durable_unknown_marker(isolated_review_factory, status, marked, blocked):
    """只有未发送的确定失败或解析完成可越过分类，已发送失败不假称远端无资源。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    async with factory() as session:
        session.add(WorkTask(work_id=work_id, kind="create_child", dedupe_key="review-child-state", status=status, dispatched_at=utc_now() if marked else None, payload={"actor_id": "100", "channel_id": "20"}))
        await session.commit()
    principal = {"id": "100", "config": {}}
    async with factory() as session:
        work = await session.get(Work, work_id)
        if blocked:
            with pytest.raises(HTTPException) as caught:
                await WorkService(session).update(work, {"is_nsfw": True}, principal, {"normal_nsfw": "30"})
            assert caught.value.status_code == 409
            assert caught.value.detail["code"] == "CHILD_CREATION_UNRESOLVED"
            await session.rollback()
        else:
            await WorkService(session).update(work, {"is_nsfw": True}, principal, {"normal_nsfw": "30"})
            await session.commit()
    async with factory() as session:
        assert (await session.get(Work, work_id)).is_nsfw is not blocked


async def install_child_actor(factory, monkeypatch, member_hook=None):
    """真实 PG 身份投影配合显式权限替身，不调用外部成员服务或 NATS。"""
    from types import SimpleNamespace
    import api.v2.config as config_module
    import work.work_permission_service as permission_module
    import work.work_task_worker as worker_module

    config = {"auth": {"role_ids": ["formal"]}, "work_forums": {"guild_id": "9", "normal": "20", "normal_nsfw": "30", "abyss": "40", "abyss_nsfw": "50"}}
    calls = {"eligibility": 0, "member": 0}
    async def eligible(*args, **kwargs):
        assert kwargs["force_authority"] is True
        calls["eligibility"] += 1
        return True
    class Permissions:
        def __init__(self, config):
            self.config = config
        async def member(self, *args, **kwargs):
            calls["member"] += 1
            assert kwargs["sensitive"] is True
            if member_hook:
                await member_hook()
            return {"roles": ["formal"]}
        async def close(self):
            return None
    monkeypatch.setattr(config_module, "get_work_config", lambda: config)
    monkeypatch.setattr(worker_module, "get_auth_service", lambda: SimpleNamespace(legacy_eligibility=eligible))
    monkeypatch.setattr(permission_module, "WorkPermissionService", Permissions)
    async with factory() as session:
        session.add(User(id="100", username="review_actor"))
        session.add(DiscordIdentity(discord_user_id="200", user_id="100", version=3, is_current=True))
        await session.commit()
    return config, calls


async def seed_child_task(factory, work_id, *, running=False):
    """待建意图刻意保留旧普通目标，用于验证首次发送时更新与重试不重绑。"""
    async with factory() as session:
        task = WorkTask(work_id=work_id, kind="create_child", dedupe_key="review-create-child", status="running" if running else "pending", attempts=1 if running else 0, payload={"actor_id": "100", "guild_id": "9", "channel_id": "20", "title": "review child", "body": "review child content", "defer_open": True, "initial_policies": []})
        session.add(task)
        await session.commit()
        return task.id


async def test_classification_during_authority_check_prevents_stale_first_dispatch(isolated_review_factory, monkeypatch):
    """资格网络等待不持锁，分类先提交后旧快照必须拒绝发送并重新取目标。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    task_id = await seed_child_task(factory, work_id, running=True)
    entered, release = asyncio.Event(), asyncio.Event()
    async def member_hook():
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
    config, _ = await install_child_actor(factory, monkeypatch, member_hook)
    worker = WorkTaskWorker(factory)
    preparation = asyncio.create_task(worker.prepare_child_dispatch(task_id, work_id, 1))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as session:
            work = await session.get(Work, work_id)
            await WorkService(session).update(work, {"is_nsfw": True}, {"id": "100", "config": {}}, config["work_forums"])
            await session.commit()
    finally:
        release.set()
    with pytest.raises(IntegrationRpcError) as caught:
        await asyncio.wait_for(preparation, 5)
    assert caught.value.code == "CHILD_CLASSIFICATION_CHANGED" and caught.value.retryable
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        assert task.dispatched_at is None and task.payload["channel_id"] == "20"
    payload = await worker.prepare_child_dispatch(task_id, work_id, 1)
    assert payload["channel_id"] == "30"
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        assert task.dispatched_at is not None and task.payload == payload


async def test_marker_is_committed_before_rpc_unknown_retry_keeps_target(isolated_review_factory, monkeypatch):
    """独立连接在 RPC 入口能看到标记，未知结果阻止分类且重试不重查换目标。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    task_id = await seed_child_task(factory, work_id)
    config, calls = await install_child_actor(factory, monkeypatch)
    async with factory() as session:
        work = await session.get(Work, work_id)
        await WorkService(session).update(work, {"is_nsfw": True}, {"id": "100", "config": {}}, config["work_forums"])
        await session.commit()
    native_calls = []
    async def rpc(subject, **fields):
        native_calls.append(dict(fields))
        assert fields["channel_id"] == "30"
        async with factory() as session:
            task = await session.get(WorkTask, task_id)
            assert task.dispatched_at is not None and task.payload["channel_id"] == "30"
            work = await session.get(Work, work_id)
            with pytest.raises(HTTPException) as caught:
                await WorkService(session).update(work, {"is_nsfw": False}, {"id": "100", "config": {}}, config["work_forums"])
            assert caught.value.status_code == 409
            await session.rollback()
        if len(native_calls) == 1:
            raise RuntimeError("fake unknown transport result")
        return {"thread_id": "801", "guild_id": "9", "channel_id": "30"}
    worker = WorkTaskWorker(factory, rpc)
    assert await worker.once()
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        assert task.status == "pending" and task.dispatched_at is not None
        task.available_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    assert await worker.once()
    assert calls == {"eligibility": 1, "member": 1}
    assert native_calls[0] == native_calls[1]
    async with factory() as session:
        assert (await session.get(WorkTask, task_id)).status == "done"
        work = await session.get(Work, work_id)
        await WorkService(session).update(work, {"is_nsfw": False}, {"id": "100", "config": {}}, config["work_forums"])
        await session.commit()
        assert not work.is_nsfw


@pytest.mark.parametrize("change,expected_code", [("binding", "CHILD_BINDING_CHANGED"), ("main", "CHILD_MAIN_UNAVAILABLE")])
async def test_local_binding_or_main_change_during_authority_blocks_marker(isolated_review_factory, monkeypatch, change, expected_code):
    """真实本地提交发生在网络资格返回前，最终临界区必须重查并拒绝旧状态。"""
    factory = isolated_review_factory
    work_id = await seed_work(factory)
    task_id = await seed_child_task(factory, work_id, running=True)
    entered, release = asyncio.Event(), asyncio.Event()
    async def member_hook():
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
    await install_child_actor(factory, monkeypatch, member_hook)
    preparation = asyncio.create_task(WorkTaskWorker(factory).prepare_child_dispatch(task_id, work_id, 1))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as session:
            if change == "binding":
                await session.execute(update(DiscordIdentity).where(DiscordIdentity.user_id == "100").values(is_current=False))
                session.add(DiscordIdentity(discord_user_id="201", user_id="100", version=4, is_current=True))
            else:
                await session.execute(update(Thread).where(Thread.work_id == work_id, Thread.role == "main").values(platform_locked=True))
            await session.commit()
    finally:
        release.set()
    with pytest.raises(IntegrationRpcError) as caught:
        await asyncio.wait_for(preparation, 5)
    assert caught.value.code == expected_code
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        assert task.dispatched_at is None and task.payload["channel_id"] == "20"


async def test_edit_soft_deleted_child_is_404_until_restore_commits(isolated_review_factory):
    """已提交软删行不满足编辑查询，恢复未提交时应404，恢复提交后才允许编辑。"""
    from types import SimpleNamespace
    from sqlalchemy.ext.asyncio import AsyncSession
    from work.work_thread_service import WorkThreadService

    factory = isolated_review_factory
    work_id = await seed_work(factory)
    async with factory() as session:
        child = Thread(platform="fluxer", thread_id=801, guild_id=9, channel_id=20, work_id=work_id, role="child", title="deleted child", body="body", fluxer_author_id="100", deleted_at=utc_now(), deletion_reason="moderation")
        session.add(child)
        await session.commit()
        child_id = child.id
    entered, release = asyncio.Event(), asyncio.Event()
    class RestoreSession(AsyncSession):
        async def get(self, model, identifier, **kwargs):
            result = await super().get(model, identifier, **kwargs)
            if model is Thread:
                assert kwargs.get("with_for_update") is True
                assert result.deleted_at is not None
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return result
    gated = async_sessionmaker(factory.kw["bind"], expire_on_commit=False, class_=RestoreSession)
    actor = {"id": "100", "discord_user_id": "200", "is_admin": True, "show_nsfw": True, "can_view_abyss": True, "can_speak": True, "config": {}}
    restore = asyncio.create_task(WorkThreadService(gated).restore_child(work_id, child_id, SimpleNamespace(actor=actor)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # READ COMMITTED先按已提交deleted_at过滤，不应等待一个未来可能恢复的行。
        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(WorkThreadService(factory).edit_child(work_id, child_id, SimpleNamespace(actor=actor, values={"locked": True})), 5)
        assert caught.value.status_code == 404
        async with factory() as session:
            assert (await session.get(Thread, child_id)).deleted_at is not None
    finally:
        release.set()
        await asyncio.wait_for(restore, 5)
    # 新语句在恢复提交后才看到可编辑行，普通锁独立提交。
    await WorkThreadService(factory).edit_child(work_id, child_id, SimpleNamespace(actor=actor, values={"locked": True}))
    async with factory() as session:
        child = await session.get(Thread, child_id)
        assert child.deleted_at is None and child.lock_owner_ids == ["100"]
        tasks = (await session.execute(select(WorkTask).where(WorkTask.work_id == work_id).order_by(WorkTask.id))).scalars().all()
        assert [(task.payload["source"], task.payload["active"]) for task in tasks] == [("forum_soft_delete", False), ("forum_work_lock", True)]


async def test_restore_and_redelete_serialize_without_replaying_ordinary_lock(isolated_review_factory):
    """删除与恢复均可命中已删行，真实行锁使后到删除在恢复提交后生效。"""
    from types import SimpleNamespace
    from sqlalchemy.ext.asyncio import AsyncSession
    from work.work_thread_service import WorkThreadService

    factory = isolated_review_factory
    work_id = await seed_work(factory)
    async with factory() as session:
        child = Thread(platform="fluxer", thread_id=802, guild_id=9, channel_id=20, work_id=work_id, role="child", title="deleted child", body="body", fluxer_author_id="100", deleted_at=utc_now(), deletion_reason="moderation", lock_owner_ids=["300"])
        session.add(child)
        await session.commit()
        child_id = child.id
    entered, release, delete_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delete_backend = {}
    class RestoreSession(AsyncSession):
        async def get(self, model, identifier, **kwargs):
            result = await super().get(model, identifier, **kwargs)
            if model is Thread:
                assert kwargs.get("with_for_update") is True
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
            return result
    class DeleteSession(AsyncSession):
        async def get(self, model, identifier, **kwargs):
            if model is Thread:
                assert kwargs.get("with_for_update") is True
                delete_backend["pid"] = (await self.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                delete_entered.set()
            return await super().get(model, identifier, **kwargs)
    gated = async_sessionmaker(factory.kw["bind"], expire_on_commit=False, class_=RestoreSession)
    deleting = async_sessionmaker(factory.kw["bind"], expire_on_commit=False, class_=DeleteSession)
    actor = {"id": "100", "discord_user_id": "200", "is_admin": True, "show_nsfw": True, "can_view_abyss": True, "can_speak": True, "config": {}}
    restore = asyncio.create_task(WorkThreadService(gated).restore_child(work_id, child_id, SimpleNamespace(actor=actor)))
    redelete = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        redelete = asyncio.create_task(WorkThreadService(deleting).delete_child(work_id, child_id, SimpleNamespace(actor=actor)))
        # 先确认同用户的第二backend已进入按主键取行，再直接观察其数据库锁等待。
        await asyncio.wait_for(delete_entered.wait(), 5)
        deadline = asyncio.get_running_loop().time() + 5
        observed_lock_wait = False
        while asyncio.get_running_loop().time() < deadline and not redelete.done():
            async with factory() as observer:
                wait_type = (await observer.execute(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": delete_backend["pid"]})).scalar_one_or_none()
            if wait_type == "Lock":
                observed_lock_wait = True
                break
            await asyncio.sleep(0.01)
        assert observed_lock_wait and not redelete.done()
    finally:
        release.set()
        await asyncio.wait_for(restore, 5)
        if redelete:
            await asyncio.wait_for(redelete, 5)
    async with factory() as session:
        child = await session.get(Thread, child_id)
        assert child.deleted_at is not None and child.lock_owner_ids == ["300"]
        tasks = (await session.execute(select(WorkTask).where(WorkTask.work_id == work_id).order_by(WorkTask.id))).scalars().all()
        assert [(task.payload["source"], task.payload["active"]) for task in tasks] == [("forum_soft_delete", False), ("forum_soft_delete", True)]
