"""真实 PostgreSQL 的保护、墓碑及共享计数回归。"""

# ruff: noqa: F811
import os
import pytest
from sqlalchemy import select, delete
from sqlalchemy.exc import DBAPIError
from models import Thread, Booklist, BooklistItem
from models.work import Work
from models.work_statistics import WorkStatistics
from work.work_event_service import WorkEventService
from work.work_service import WorkService
from test_work_v2_postgres import factory, principal, client, seed_thread  # noqa: F401

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_WORK_DB_URL"), reason="仅显式隔离数据库"
    ),
]


async def test_physical_delete_rolls_back_preceding_relation_changes(
    factory, principal
):
    """旧删除事务必须整体回滚，不能先丢收藏再拒绝删除。"""
    work_id, row_id = await seed_thread(factory, external=991001)
    async with factory() as session:
        work = await session.get(Work, work_id)
        await WorkService(session).collect(work, principal, True)
        await session.commit()
        item = (
            await session.execute(
                select(BooklistItem).where(BooklistItem.work_id == work_id)
            )
        ).scalar_one()
        item_id = item.id
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    delete(BooklistItem).where(BooklistItem.id == item_id)
                )
                await session.execute(delete(Thread).where(Thread.id == row_id))
        assert (
            await session.get(BooklistItem, item_id, populate_existing=True) is not None
        )
        assert await session.get(Thread, row_id) is not None


async def test_deleted_before_creation_keeps_tombstone(factory):
    """权威删除先到时，旧或更高普通快照均不能复活主帖。"""
    async with factory() as session:
        service = WorkEventService(session, {"guild_id": "999", "normal": "200"})
        event = {
            "event_id": "delete-first-991002",
            "platform": "fluxer",
            "thread_id": "991002",
            "guild_id": "999",
            "channel_id": "200",
            "state_version": 2,
            "deleted": True,
        }
        assert await service.apply(event)
        event.update(
            event_id="changed-late-991002",
            state_version=3,
            deleted=False,
            title="不可复活",
        )
        assert not await service.apply(event)
        assert (
            not (
                await session.execute(
                    select(Thread).where(
                        Thread.platform == "fluxer", Thread.thread_id == 991002
                    )
                )
            )
            .scalars()
            .first()
        )


async def test_collection_reassignment_refreshes_old_and_new(factory, principal):
    """关联条目更换作品后旧新计数都立即刷新。"""
    old, _ = await seed_thread(factory, external=991003)
    new, _ = await seed_thread(factory, external=991004)
    async with factory() as session:
        await WorkService(session).collect(
            await session.get(Work, old), principal, True
        )
        await session.commit()
        item = (
            await session.execute(
                select(BooklistItem).where(BooklistItem.work_id == old)
            )
        ).scalar_one()
        item.work_id = new
        item.thread_id = 991004
        await session.commit()
        assert (
            await session.get(WorkStatistics, old, populate_existing=True)
        ).collection_count == 0
        assert (
            await session.get(WorkStatistics, new, populate_existing=True)
        ).collection_count == 1
        booklist = await session.get(Booklist, item.booklist_id)
        booklist.is_default = False
        await session.commit()
        assert (
            await session.get(WorkStatistics, new, populate_existing=True)
        ).collection_count == 0


async def test_child_counts_do_not_change_main_mirror(factory):
    """讨论子帖统计不叠加进作品主帖镜像。"""
    work_id, row_id = await seed_thread(factory, external=991005)
    async with factory() as session:
        main = await session.get(Thread, row_id)
        main.reaction_count = 2
        main.reply_count = 3
        session.add(
            Thread(
                platform="fluxer",
                thread_id=991006,
                guild_id=999,
                channel_id=200,
                title="讨论",
                work_id=work_id,
                role="child",
                reaction_count=99,
                reply_count=98,
            )
        )
        await session.commit()
        stats = await session.get(WorkStatistics, work_id, populate_existing=True)
        assert (stats.reaction_count, stats.reply_count) == (2, 3)


async def test_missing_mapping_is_repaired_after_authority_arrives(factory):
    """旧写入早于身份映射时由持久列桶追增，不依赖进程内事件。"""
    from models import User, DiscordIdentity
    from identity.identity_projection_service import IdentityProjectionService

    async with factory() as session:
        row = Thread(
            platform="discord",
            thread_id=991007,
            guild_id=999,
            channel_id=200,
            title="晚到映射",
            author_id=103,
        )
        session.add(row)
        await session.commit()
        assert row.fluxer_author_id is None
        session.add_all(
            [
                User(id="u3", username="late"),
                DiscordIdentity(discord_user_id="103", user_id="u3"),
            ]
        )
        await session.commit()
        assert await IdentityProjectionService(session).once() >= 1
        await session.commit()
        await session.refresh(row)
        assert row.fluxer_author_id == "u3"
        assert (
            await session.get(Work, row.work_id, populate_existing=True)
        ).author_id == "u3"


async def test_legacy_preferences_roundtrip_keeps_native_authors(factory, monkeypatch):
    """V1 wire只用Discord身份，原样保存不会误解释或丢纯原生筛选。"""
    from models import User
    from core.preferences_repository import PreferencesRepository
    from identity.legacy_api_adapter import LegacyApiAdapter
    from identity.runtime import request_identity_mode

    monkeypatch.setenv("FORUM_V2_ENABLED", "true")
    async with factory() as session:
        session.add(User(id="native-only", username="native"))
        await session.commit()
        repo = PreferencesRepository(session)
        await repo.save_user_preferences(
            "u1", {"include_authors": ["u2", "native-only"]}, 999
        )
        marker = request_identity_mode.set("legacy")
        try:
            current = await repo.get_user_preferences(101, 999)
            adapter = LegacyApiAdapter(session)
            wire = await adapter.preferences_to_wire(current, 101)
            assert list(map(str, wire.include_authors)) == ["102"]
            data = await adapter.preserve_native_preferences(
                {"include_authors": wire.include_authors}, current
            )
            saved = await repo.save_user_preferences(101, data, 999)
            assert list(map(str, saved.include_authors)) == ["u2", "native-only"]
        finally:
            request_identity_mode.reset(marker)


async def test_seven_day_proposal_uses_shared_tag_binding(factory, monkeypatch):
    """后台期限处理使用原审核表，并让V1搜索命中新挂作品标签。"""
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from models import Tag
    from models.tag_proposal import TagProposal
    from models.tag_binding import TagBinding
    from shared.time_utils import utc_now
    from tag.tag_worker import TagWorker
    from core.tag_query import tag_filters
    from sqlalchemy.orm import selectinload

    work_id, row_id = await seed_thread(factory, external=991008)
    monkeypatch.setattr(
        "core.tag_access_service.TagAccessService.roles",
        AsyncMock(return_value={"formal"}),
    )
    async with factory() as session:
        tag = Tag(name="期限审核作品标签", source="custom", category=2)
        session.add(tag)
        await session.flush()
        proposal = TagProposal(
            target_type="work",
            target_id=work_id,
            work_id=work_id,
            tag_id=tag.id,
            applicant_id=102,
            fluxer_applicant_id="u2",
            owner_id=101,
            fluxer_owner_id="u1",
            due_at=utc_now() - timedelta(seconds=1),
            created_at=utc_now() - timedelta(days=7, seconds=1),
        )
        session.add(proposal)
        await session.commit()
        proposal_id, tag_id = proposal.id, tag.id
    await TagWorker(
        factory, {"main_guild_id": 999, "auth": {"role_ids": ["formal"]}}
    ).expire()
    async with factory() as session:
        proposal = await session.get(TagProposal, proposal_id)
        assert proposal.status == "approved"
        binding = (
            await session.execute(
                select(TagBinding).where(TagBinding.work_id == work_id)
            )
        ).scalar_one()
        assert binding.target_type == "work"
        rows = (
            (
                await session.execute(
                    select(Thread)
                    .where(
                        Thread.id == row_id,
                        *tag_filters("thread", Thread.id, [tag_id], []),
                    )
                    .options(selectinload(Thread.tags))
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1 and tag_id in [tag.id for tag in rows[0].tags]


async def test_transient_source_failure_preserves_visibility(factory):
    """普通读取失败计数不改变作品可见性，初始隐藏来源仍应隐藏。"""
    work_id, row_id = await seed_thread(factory, external=991009)
    async with factory() as session:
        row = await session.get(Thread, row_id)
        row.not_found_count = 12
        await session.commit()
        assert (await session.get(Work, work_id, populate_existing=True)).source_visible
        hidden = Thread(
            platform="discord",
            thread_id=991010,
            guild_id=999,
            channel_id=200,
            title="历史隐藏",
            show_flag=False,
        )
        session.add(hidden)
        await session.commit()
        await session.refresh(hidden)
        assert not (await session.get(Work, hidden.work_id)).source_visible


async def test_late_rebind_rpc_result_does_not_clear_new_current_binding(factory):
    """更晚版本事件先到后，旧RPC完成不能撤销当前绑定。"""
    from identity.binding_projection_service import BindingProjectionService
    from work.work_task_worker import WorkTaskWorker
    from models.work_task import WorkTask
    from models import DiscordIdentity

    async with factory() as session:
        assert await BindingProjectionService(session).apply(
            {"user_id": "u1", "discord_user_id": "111", "version": 10}
        )
        task = WorkTask(
            work_id=0,
            kind="managed_rebind",
            dedupe_key="late-rebind-boundary",
            payload={"actor_id": "u2"},
        )
        session.add(task)
        await session.flush()
        await WorkTaskWorker().apply_result(
            session, task, {"user_id": "u1", "discord_user_id": "110", "version": 9}
        )
        await session.commit()
        current = (
            await session.execute(
                select(DiscordIdentity).where(
                    DiscordIdentity.user_id == "u1",
                    DiscordIdentity.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert current.discord_user_id == "111" and current.version == 10


async def test_child_initializing_waits_for_all_mute_dependencies(factory, principal):
    """DC作品禁言只投影Fluxer子帖，开放依赖完整保留。"""
    from models.work_task import WorkTask
    from work.work_moderation_service import WorkModerationService

    work_id, _ = await seed_thread(factory, external=991011)
    async with factory() as session:
        work = await session.get(Work, work_id)
        service = WorkModerationService(session)
        await service.mute(work, principal, "u2", "只限制讨论区")
        child = Thread(
            platform="fluxer",
            thread_id=991012,
            guild_id=999,
            channel_id=200,
            title="新子帖",
            work_id=work_id,
            role="child",
            initializing=True,
        )
        session.add(child)
        await session.flush()
        initial = await service.initial_policies(work_id, 100)
        assert len(initial) == 1 and initial[0]["target_user_id"] == "u2"
        await service.reconcile_child(
            child, {"actor_id": "u1", "defer_open": True, "initial_policies": initial}
        )
        await session.commit()
        tasks = (
            (
                await session.execute(
                    select(WorkTask).where(
                        WorkTask.work_id == work_id, WorkTask.kind == "policy"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert tasks and all(task.payload["thread_id"] == "991012" for task in tasks)
        release = [
            task for task in tasks if task.payload["source"] == "forum_initializing"
        ][0]
        assert release.payload["depends_on"]
        assert child.initializing


async def test_late_source_binding_after_merge_cannot_resurrect_account(factory):
    """被合并账户的迟到绑定事件不可建立新的当前登录身份。"""
    from identity.binding_projection_service import BindingProjectionService
    from identity.identity_merge_service import IdentityMergeService
    from models import DiscordIdentity

    async with factory() as session:
        await IdentityMergeService(session).merge(
            "u1", "u2", "source-late-merge-operation", "u2"
        )
        assert not await BindingProjectionService(session).apply(
            {"user_id": "u1", "discord_user_id": "119", "version": 100}
        )
        assert await session.get(DiscordIdentity, "119") is None


async def test_discord_abyss_category_follows_existing_channel_authority(
    factory, client, principal
):
    """Discord深渊来自原配置，不能靠未回填标志或手工编辑绕开。"""
    work_id, _ = await seed_thread(factory, external=991013)
    principal["config"] = {"abyss": {"channel_ids": ["200"]}}
    principal["can_view_abyss"] = False
    assert (await client.get(f"/v2/works/{work_id}")).status_code == 404
    assert (await client.get("/v2/works")).json()["total"] == 0
    from models import Notification

    async with factory() as session:
        session.add(
            Notification(
                user_id=101,
                fluxer_user_id="u1",
                work_id=work_id,
                thread_id=991013,
                event_type="author_new_thread",
                event_source_id=991013,
                target_type="thread",
                target_id=991013,
            )
        )
        await session.commit()
    assert (await client.get("/v2/me/notifications")).json()["total"] == 0
    principal["can_view_abyss"] = True
    assert (await client.get(f"/v2/works/{work_id}")).json()["is_abyss"] is True
    assert (
        await client.patch(f"/v2/works/{work_id}", json={"is_abyss": False})
    ).status_code == 403


async def test_legacy_bulk_booklist_change_keeps_hidden_native_items(
    factory, principal, monkeypatch
):
    """V1局部批量更新不会清除旧前端无法看到的原生NSFW收藏。"""
    from booklist.booklist_service import BooklistService
    from identity.runtime import request_identity_mode

    discord_work, _ = await seed_thread(factory, external=991014)
    native_work, _ = await seed_thread(factory, external=991015, platform="fluxer")
    async with factory() as session:
        service = WorkService(session)
        await service.collect(await session.get(Work, discord_work), principal, True)
        native = await session.get(Work, native_work)
        native.is_nsfw = True
        await service.collect(native, principal, True)
        await session.commit()
        booklist = (
            await session.execute(
                select(Booklist).where(
                    Booklist.is_default.is_(True), Booklist.fluxer_owner_id == "u1"
                )
            )
        ).scalar_one()
        booklist_id = booklist.id
        monkeypatch.setenv("FORUM_V2_ENABLED", "true")
        marker = request_identity_mode.set("legacy")
        try:
            await BooklistService(session).sync_thread_in_booklists(
                101, 991014, [booklist_id], []
            )
            await session.commit()
        finally:
            request_identity_mode.reset(marker)
        items = (
            (
                await session.execute(
                    select(BooklistItem).where(BooklistItem.booklist_id == booklist_id)
                )
            )
            .scalars()
            .all()
        )
        assert (
            len(items) == 1
            and items[0].work_id == native_work
            and items[0].thread_id is None
        )


async def test_concurrent_legacy_default_booklist_creation_is_one_fact():
    """真实独立连接并发创建默认书单时等待同一锁而不是向用户报重复。"""
    import asyncio
    import uuid
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from identity.canonical_user_id import CanonicalUserId
    from identity.runtime import request_identity_mode
    from core.booklist_repository import BooklistRepository

    owner = "default-race-" + uuid.uuid4().hex
    engine = create_async_engine(os.environ["TEST_WORK_DB_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    marker = request_identity_mode.set("fluxer")

    async def create():
        async with sessions() as session:
            return (
                await BooklistRepository(session).get_or_create_default_booklist(
                    CanonicalUserId(owner)
                )
            ).id

    try:
        ids = await asyncio.gather(create(), create())
        assert ids[0] == ids[1]
    finally:
        async with sessions() as session:
            await session.execute(
                delete(Booklist).where(Booklist.fluxer_owner_id == owner)
            )
            await session.commit()
        request_identity_mode.reset(marker)
        await engine.dispose()
