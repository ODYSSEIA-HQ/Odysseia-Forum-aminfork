"""隔离 Windows PostgreSQL 上验证作品层真实 SQL 与 HTTP 行为。"""

import os
from datetime import timedelta
from unittest.mock import AsyncMock
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text, update, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from api.v2.routers import works
from api.v2.dependencies import current_member
from models import (
    Thread,
    User,
    DiscordIdentity,
    Booklist,
    BooklistItem,
    ThreadFollow,
    Notification,
)
from models.work import Work
from models.work_statistics import WorkStatistics
from models.work_task import WorkTask
from work.work_task_worker import WorkTaskWorker
from work.work_service import WorkService
from shared.time_utils import utc_now

URL = os.environ.get("TEST_WORK_DB_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="需要显式独立 TEST_WORK_DB_URL，禁止访问默认数据库"
)


@pytest_asyncio.fixture
async def factory(monkeypatch):
    """每个用例在独立根事务内，可提交业务保存点且最终回滚。"""
    from identity.runtime import request_identity_mode

    marker = request_identity_mode.set("fluxer")
    engine = create_async_engine(URL)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        monkeypatch.setattr(works, "AsyncSessionFactory", factory)
        async with factory() as session:
            session.add_all(
                [
                    User(id="u1", username="author"),
                    User(id="u2", username="reader"),
                    DiscordIdentity(discord_user_id="101", user_id="u1"),
                    DiscordIdentity(discord_user_id="102", user_id="u2"),
                ]
            )
            await session.commit()
        yield factory
        await transaction.rollback()
    await engine.dispose()
    request_identity_mode.reset(marker)


@pytest.fixture
def principal():
    """当前成员能力，可按测试切换访问主体。"""
    return {
        "id": "u1",
        "discord_user_id": "101",
        "username": "author",
        "display_name": None,
        "avatar_url": None,
        "show_nsfw": True,
        "is_admin": False,
        "can_view_abyss": True,
        "can_create_work": True,
        "can_speak": True,
        "forums": {
            "guild_id": "999",
            "normal": "200",
            "normal_nsfw": "201",
            "abyss": "202",
            "abyss_nsfw": "203",
        },
    }


@pytest_asyncio.fixture
async def client(factory, principal):
    """通过实际 FastAPI 路由运行，隔离外部认证 HTTP。"""
    app = FastAPI()
    app.include_router(works.router)
    app.dependency_overrides[current_member] = lambda: principal
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://forum.test"
    ) as client:
        yield client


async def seed_thread(factory, external=500, platform="discord"):
    """按旧 writer 字段插入，依赖真实触发器创建作品。"""
    async with factory() as session:
        row = Thread(
            thread_id=external,
            platform=platform,
            guild_id=999,
            channel_id=200,
            title="旧来源作品",
            author_id=101,
            fluxer_author_id="u1",
            reaction_count=7,
            reply_count=3,
            thumbnail_urls=["https://image.test/a.png"],
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        work_id = row.work_id
        identifier = row.id
        await session.commit()
        return work_id, identifier


@pytest.mark.asyncio
async def test_old_writer_projection_and_platform_identity(factory):
    """旧写新增更新持续投影，不依赖迁移扫描高水位。"""
    work_id, thread_id = await seed_thread(factory)
    second, _ = await seed_thread(factory, platform="fluxer")
    assert second != work_id
    async with factory() as session:
        await session.execute(
            update(Thread)
            .where(Thread.id == thread_id)
            .values(title="旧进程改名", reaction_count=11)
        )
        await session.commit()
        work = await session.get(Work, work_id)
        stats = await session.get(WorkStatistics, work_id)
        assert work.title == "旧进程改名"
        assert work.images == ["https://image.test/a.png"]
        assert stats.reaction_count == 11
        tasks = (
            (await session.execute(select(WorkTask).where(WorkTask.work_id == work_id)))
            .scalars()
            .all()
        )
        assert tasks
        constraints = (
            await session.execute(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE contype='f' AND connamespace='public'::regnamespace"
                )
            )
        ).scalar_one()
        assert constraints == 0


@pytest.mark.asyncio
async def test_nsfw_detail_and_pagination_share_filter(client, factory, principal):
    """隐藏作品既不能查看详情也不会泄漏分页总数。"""
    work_id, _ = await seed_thread(factory)
    async with factory() as session:
        await session.execute(
            update(Work).where(Work.id == work_id).values(is_nsfw=True)
        )
        await session.commit()
    principal["show_nsfw"] = False
    response = await client.get("/v2/works")
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert (await client.get(f"/v2/works/{work_id}")).status_code == 404
    principal["show_nsfw"] = True
    assert (await client.get("/v2/works")).json()["total"] == 1


@pytest.mark.asyncio
async def test_collection_reuses_legacy_default_and_counts_once(client, factory):
    """旧条目与 V2 收藏共享默认书单和唯一事实。"""
    work_id, _ = await seed_thread(factory)
    async with factory() as session:
        book = Booklist(
            title="默认收藏",
            owner_id=101,
            fluxer_owner_id="u1",
            is_default=True,
            is_public=False,
        )
        session.add(book)
        await session.flush()
        session.add(BooklistItem(booklist_id=book.id, owner_id=101, thread_id=500))
        await session.commit()
    for _ in range(2):
        response = await client.post(f"/v2/works/{work_id}/collection")
        assert response.status_code == 200, response.text
    async with factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(Booklist))
        ).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(BooklistItem))
        ).scalar_one() == 1
        assert (await session.get(WorkStatistics, work_id)).collection_count == 1
    assert (await client.delete(f"/v2/works/{work_id}/collection")).status_code == 200
    async with factory() as session:
        assert (await session.get(WorkStatistics, work_id)).collection_count == 0


@pytest.mark.asyncio
async def test_view_redis_nx_and_pg_count(factory, principal):
    """同用户二十四小时仅一次浏览，不创建 PG 去重表。"""
    work_id, _ = await seed_thread(factory)
    redis = AsyncMock()
    redis.set.side_effect = [True, None]
    async with factory() as session:
        service = WorkService(session)
        work = await service.visible(work_id, principal)
        assert await service.view(work, principal, redis) == {"counted": True}
        assert await service.view(work, principal, redis) == {"counted": False}
        assert redis.set.call_args.kwargs == {"nx": True, "ex": 86400}
        assert (await session.get(WorkStatistics, work_id)).view_count == 1


@pytest.mark.asyncio
async def test_native_child_creation_worker_retry_and_idempotence(
    client, factory, principal
):
    """子帖创建先持久提交任务，重试复用 operation_id 并仅投影一次。"""
    work_id, _ = await seed_thread(factory)
    response = await client.post(
        f"/v2/works/{work_id}/threads", json={"title": "子帖", "body": "原生正文"}
    )
    assert response.status_code == 202, response.text
    task_id = int(response.json()["task_id"])
    async with factory() as session:
        await session.execute(
            update(WorkTask).where(WorkTask.kind == "projection").values(status="done")
        )
        await session.commit()
    rpc = AsyncMock(
        side_effect=[
            RuntimeError("offline"),
            {
                "guild_id": "999",
                "channel_id": "200",
                "thread_id": "888",
                "event_id": "e1",
                "state_version": 1,
            },
        ]
    )
    worker = WorkTaskWorker(factory, rpc)
    await worker.once()
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        assert task.status == "pending" and task.attempts == 1
        task.available_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    await worker.once()
    assert (
        rpc.call_args_list[0].kwargs["operation_id"]
        == rpc.call_args_list[1].kwargs["operation_id"]
    )
    async with factory() as session:
        task = await session.get(WorkTask, task_id)
        assert task.status == "done"
        threads = (
            (await session.execute(select(Thread).where(Thread.work_id == work_id)))
            .scalars()
            .all()
        )
        assert len(threads) == 2
        assert {row.platform for row in threads} == {"discord", "fluxer"}


@pytest.mark.asyncio
async def test_announcement_snapshot_edit_withdraw_and_shared_read(
    client, factory, principal
):
    """发布时捕获关注者，编辑不新增通知，撤回更新原通知展示。"""
    work_id, _ = await seed_thread(factory)
    async with factory() as session:
        session.add(
            ThreadFollow(
                user_id=102,
                fluxer_user_id="u2",
                thread_id=500,
                work_id=work_id,
                active_flag=True,
            )
        )
        await session.commit()
    response = await client.post(
        f"/v2/works/{work_id}/announcements", json={"body": "正文"}
    )
    assert response.status_code == 200, response.text
    announcement = response.json()["id"]
    assert (
        await client.patch(
            f"/v2/works/{work_id}/announcements/{announcement}", json={"body": "修改"}
        )
    ).status_code == 200
    assert (
        await client.delete(f"/v2/works/{work_id}/announcements/{announcement}")
    ).status_code == 200
    principal.update(id="u2", discord_user_id="102")
    response = await client.get("/v2/me/notifications")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 1
    assert result["items"][0]["withdrawn"] is True
    notice = result["items"][0]["id"]
    assert (await client.post(f"/v2/me/notifications/{notice}/read")).status_code == 200
    async with factory() as session:
        row = await session.get(Notification, int(notice))
        assert row.read_at is not None
        assert row.user_id == 102 and row.fluxer_user_id == "u2"


@pytest.mark.asyncio
async def test_history_binding_preserves_ownership_and_old_event_rejected(
    factory, monkeypatch
):
    """解绑后历史归属保留，乱序事件不会重新激活旧身份。"""
    import shared.database
    from identity.fluxer_auth_service import FluxerAuthService

    monkeypatch.setattr(shared.database, "AsyncSessionFactory", factory)
    service = FluxerAuthService(AsyncMock())
    service.call_rpc = AsyncMock(
        return_value={"profiles": [{"user_id": "u1", "username": "author"}]}
    )
    await service.apply_binding(
        {"user_id": "u1", "discord_user_id": "103", "version": 2}
    )
    await service.apply_binding(
        {"user_id": "u1", "discord_user_id": "101", "version": 1}
    )
    async with factory() as session:
        old = await session.get(DiscordIdentity, "101")
        current = await session.get(DiscordIdentity, "103")
        assert old.user_id == "u1" and old.is_current is False
        assert current.user_id == "u1" and current.is_current is True


@pytest.mark.asyncio
async def test_managed_merge_keeps_publishers_and_conflict_notes(factory):
    """管理合并迁移业务而不改历史发布者，并保留重复条目备注及旧链接。"""
    from identity.identity_merge_service import IdentityMergeService
    from models.work_announcement import WorkAnnouncement
    from models.operation_log import OperationLog
    from core.booklist_repository import BooklistRepository

    work_id, _ = await seed_thread(factory)
    async with factory() as session:
        old = Booklist(
            title="旧默认", owner_id=101, fluxer_owner_id="u1", is_default=True
        )
        current = Booklist(
            title="目标默认", owner_id=102, fluxer_owner_id="u2", is_default=True
        )
        session.add_all([old, current])
        await session.flush()
        old_id, target_id = old.id, current.id
        session.add_all(
            [
                BooklistItem(
                    booklist_id=old.id,
                    work_id=work_id,
                    thread_id=500,
                    owner_id=101,
                    fluxer_owner_id="u1",
                    comment="原有独立备注",
                ),
                BooklistItem(
                    booklist_id=current.id,
                    work_id=work_id,
                    thread_id=500,
                    owner_id=102,
                    fluxer_owner_id="u2",
                    comment="目标备注",
                ),
                WorkAnnouncement(work_id=work_id, author_id="u1", body="历史发布"),
                OperationLog(
                    type="tag.bind",
                    target_type="work",
                    target_id=work_id,
                    fluxer_actor_id="u1",
                    actor_id=101,
                    detail={"original": True},
                ),
            ]
        )
        await session.flush()
        assert (
            await IdentityMergeService(session).merge(
                "u1", "u2", "managed-operation-1", "u2"
            )
            is True
        )
        await session.commit()
        assert (await session.get(Work, work_id)).author_id == "u2"
        announcement = (
            (
                await session.execute(
                    select(WorkAnnouncement).where(WorkAnnouncement.work_id == work_id)
                )
            )
            .scalars()
            .one()
        )
        assert announcement.author_id == "u1"
        audit = (
            (
                await session.execute(
                    select(OperationLog).where(OperationLog.type == "tag.bind")
                )
            )
            .scalars()
            .one()
        )
        assert audit.fluxer_actor_id == "u1" and audit.actor_id == 101
        collisions = (
            (
                await session.execute(
                    select(OperationLog).where(
                        OperationLog.type == "identity.merge_booklist_item"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert collisions[0].detail["original_item"]["comment"] == "原有独立备注"
        assert (await BooklistRepository(session).get_booklist(old_id)).id == target_id
        assert (
            await IdentityMergeService(session).merge(
                "u1", "u2", "managed-operation-1", "u2"
            )
            is False
        )


@pytest.mark.asyncio
async def test_native_snapshot_auto_work_emoji_and_out_of_order(factory):
    """原生论坛发帖自动生成主作品，永久幂等与独立版本拒绝旧快照。"""
    from work.work_event_service import WorkEventService
    from models.work_reaction import WorkReaction

    event = {
        "event_id": "native-1",
        "platform": "fluxer",
        "guild_id": "999",
        "channel_id": "200",
        "thread_id": "900",
        "state_version": 1,
        "snapshot": {
            "title": "原生作品",
            "body": "中文简介 searchable",
            "author_id": "u1",
            "images": ["https://image.test/native.png"],
            "reactions": [
                {"emoji": {"id": None, "name": "🔥", "animated": False}, "count": 4}
            ],
            "reply_count": 2,
            "archived": False,
            "locked": False,
        },
    }
    async with factory() as session:
        service = WorkEventService(session, {"guild_id": "999", "normal": "200"})
        assert await service.apply(event) is True
        assert await service.apply(event) is False
        thread = (
            (
                await session.execute(
                    select(Thread).where(
                        Thread.platform == "fluxer", Thread.thread_id == 900
                    )
                )
            )
            .scalars()
            .one()
        )
        work = await session.get(Work, thread.work_id)
        assert work.title == "原生作品" and thread.role == "main"
        reaction = (
            (
                await session.execute(
                    select(WorkReaction).where(WorkReaction.work_id == work.id)
                )
            )
            .scalars()
            .one()
        )
        assert reaction.emoji_data["name"] == "🔥" and reaction.count == 4
        old = {
            **event,
            "event_id": "native-stale",
            "snapshot": {**event["snapshot"], "title": "乱序旧标题"},
        }
        assert await service.apply(old) is False
        await session.commit()
        assert (await session.get(Work, work.id)).title == "原生作品"


@pytest.mark.asyncio
async def test_native_images_require_upload_and_remain_authoritative(client, factory):
    """原生作品不接受任意URL伪装附件，事件镜像保持真实原生首图。"""
    from work.work_event_service import WorkEventService

    work_id, _ = await seed_thread(factory, platform="fluxer")
    response = await client.patch(
        f"/v2/works/{work_id}", json={"images": ["https://image.test/user-choice.png"]}
    )
    assert response.status_code == 422, response.text
    async with factory() as session:
        await WorkEventService(session).apply(
            {
                "event_id": "native-image-change",
                "platform": "fluxer",
                "thread_id": "500",
                "state_version": 1,
                "snapshot": {
                    "images": ["https://cdn.local/new.png"],
                    "author_id": "u1",
                },
            }
        )
        await session.commit()
        assert (await session.get(Work, work_id)).images == [
            "https://cdn.local/new.png"
        ]


@pytest.mark.asyncio
async def test_work_mute_blocks_child_but_keeps_collection(client, factory, principal):
    """作品禁言拒绝创建子帖但不限制收藏。"""
    work_id, _ = await seed_thread(factory, platform="fluxer")
    response = await client.post(
        f"/v2/works/{work_id}/mutes", json={"user_id": "u2", "reason": "暂停讨论"}
    )
    assert response.status_code == 202, response.text
    principal.update(id="u2", discord_user_id="102")
    response = await client.post(
        f"/v2/works/{work_id}/threads", json={"title": "禁止新建", "body": "正文"}
    )
    assert response.status_code == 403, response.text
    assert (await client.post(f"/v2/works/{work_id}/collection")).status_code == 200
    async with factory() as session:
        tasks = (
            (await session.execute(select(WorkTask).where(WorkTask.kind == "policy")))
            .scalars()
            .all()
        )
        assert tasks and all(
            row.payload["source"] == "forum_work_mute" for row in tasks
        )
        assert all(row.payload["target_user_id"] == "u2" for row in tasks)


@pytest.mark.asyncio
async def test_soft_restore_preserves_native_state_and_source_delete_is_final(
    client, factory, principal
):
    """普通隐藏可恢复且不清原生锁，真实源主帖删除不能恢复。"""
    from work.work_event_service import WorkEventService

    work_id, thread_id = await seed_thread(factory, platform="fluxer")
    async with factory() as session:
        await session.execute(
            update(Thread)
            .where(Thread.id == thread_id)
            .values(platform_locked=True, archived=True)
        )
        await session.commit()
    assert (await client.delete(f"/v2/works/{work_id}")).status_code == 200
    assert (await client.get(f"/v2/works/{work_id}")).status_code == 404
    principal["is_admin"] = True
    assert (await client.post(f"/v2/works/{work_id}/restore")).status_code == 200
    async with factory() as session:
        row = await session.get(Thread, thread_id)
        assert row.platform_locked and row.archived
        await WorkEventService(session).apply(
            {
                "event_id": "deleted-main",
                "platform": "fluxer",
                "thread_id": "500",
                "state_version": 1,
                "deleted": True,
            }
        )
        await session.commit()
    assert (await client.post(f"/v2/works/{work_id}/restore")).status_code == 403
    response = await client.get("/v2/admin/deleted-works")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["can_restore"] is False


@pytest.mark.asyncio
async def test_v1_v2_collection_bidirectional_without_id_guessing(
    client, factory, principal, monkeypatch
):
    """V1 声明 Discord 主体写入后 V2 立即读取，反向亦共用同一默认书单。"""
    from identity.runtime import request_identity_mode
    from core.collection_repository import CollectionRepository
    from shared.enum import CollectionType

    monkeypatch.setenv("FORUM_V2_ENABLED", "true")
    monkeypatch.setattr(
        CollectionRepository, "_record_thread_collection_trends", AsyncMock()
    )
    work_id, _ = await seed_thread(factory)
    marker = request_identity_mode.set("legacy")
    try:
        async with factory() as session:
            result = await CollectionRepository(session).add_collections(
                101, CollectionType.THREAD, [500]
            )
            assert result.added_count == 1
    finally:
        request_identity_mode.reset(marker)
    response = await client.get("/v2/me/collection")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["id"] == str(work_id)
    assert (await client.delete(f"/v2/works/{work_id}/collection")).status_code == 200
    assert (await client.post(f"/v2/works/{work_id}/collection")).status_code == 200
    marker = request_identity_mode.set("legacy")
    try:
        async with factory() as session:
            ids = await CollectionRepository(session).get_collected_target_ids(
                101, CollectionType.THREAD, [500]
            )
            assert ids == {500}
            defaults = (
                (
                    await session.execute(
                        select(Booklist).where(
                            Booklist.fluxer_owner_id == "u1",
                            Booklist.is_default.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(defaults) == 1
    finally:
        request_identity_mode.reset(marker)


@pytest.mark.asyncio
async def test_fts_grouping_description_and_exemption(client, factory):
    """作品关键词沿用共享全文语义，简介参与查询而非仅标题子串。"""
    work_id, thread_id = await seed_thread(factory)
    async with factory() as session:
        row = await session.get(Thread, thread_id)
        row.title = "alpha"
        row.first_message_excerpt = "beta gamma 禁 taboo"
        await session.commit()
    response = await client.post(
        "/v2/search", json={"keywords": "alpha beta", "sort_method": "created_at"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    response = await client.post(
        "/v2/search",
        json={
            "keywords": "gamma",
            "exclude_keywords": "taboo",
            "exclude_keyword_exemption_markers": ["禁"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    response = await client.post(
        "/v2/search", json={"keywords": "missing", "sort_method": "reddit_hot"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_durable_auth_inbox_commits_only_after_invalidation(factory):
    """失效未完成不能记已处理，重送最终仅持久一次。"""
    import json
    from types import SimpleNamespace
    from work.work_event_consumer import WorkEventConsumer
    from models.work_event import WorkEvent

    message = SimpleNamespace(
        subject="events.auth.v1.user_invalidated",
        data=json.dumps({"event_id": "auth-durable-1", "user_id": "u1"}).encode(),
    )
    auth = SimpleNamespace(ready=False, invalidated=AsyncMock())
    consumer = WorkEventConsumer(factory, auth)
    with pytest.raises(RuntimeError):
        await consumer.process("auth", message, auth)
    async with factory() as session:
        assert await session.get(WorkEvent, "auth-durable-1") is None
    auth.ready = True
    await consumer.process("auth", message, auth)
    await consumer.process("auth", message, auth)
    assert auth.invalidated.await_count == 2


def test_self_hosted_native_thread_refs(monkeypatch):
    """来源链接使用部署域名，缺配置时不跳转上游官方站。"""
    from shared.platform_thread_ref import fluxer_thread_ref

    monkeypatch.setenv("FLUXER_WEB_URL", "https://chat.example.test/native/")
    assert (
        fluxer_thread_ref("10", "20")
        == "https://chat.example.test/native/channels/10/20"
    )
    monkeypatch.delenv("FLUXER_WEB_URL")
    monkeypatch.delenv("FLUXER_API_URL", raising=False)
    assert fluxer_thread_ref("10", "20") == ""
