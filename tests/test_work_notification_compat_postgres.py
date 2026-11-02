"""新旧公告通知在独立 PostgreSQL 中共享事实与命名空间。"""

# ruff: noqa: F811
# pytest 显式复用集成 fixture。
import os
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from models import (
    Notification,
    ThreadUpdate,
    ThreadFollow,
    AuthorFollow,
    User,
    DiscordIdentity,
)
from models.work_announcement import WorkAnnouncement
from models.work_task import WorkTask
from core.notification_fanout_service import NotificationFanoutService
from core.notification_repository import NotificationRepository
from shared.time_utils import utc_now
from test_work_v2_postgres import factory, principal, client, seed_thread  # noqa: F401

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_WORK_DB_URL"), reason="仅使用显式隔离测试数据库"
    ),
]


async def test_announcement_bounded_history_snapshot_and_no_repeat(
    client, factory, principal
):
    """发布时事务快照包含旧身份关注者，编辑撤回不补通知，历史有界分页。"""
    work_id, _ = await seed_thread(factory, external=960)
    async with factory() as session:
        session.add(
            ThreadFollow(
                work_id=work_id,
                thread_id=960,
                user_id=102,
                fluxer_user_id=None,
                followed_at=utc_now() - timedelta(days=1),
            )
        )
        await session.commit()
        before = (
            await session.execute(select(func.count()).select_from(WorkTask))
        ).scalar_one()
    first = await client.post(
        f"/v2/works/{work_id}/announcements", json={"body": "首次公告", "images": []}
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/v2/works/{work_id}/announcements", json={"body": "独立第二次公告"}
    )
    assert second.status_code == 200
    history = (
        await client.get(
            f"/v2/works/{work_id}/announcements", params={"limit": 1, "offset": 1}
        )
    ).json()
    assert (
        history["total"] == 2 and len(history["items"]) == 1 and history["offset"] == 1
    )
    assert (
        await client.get(f"/v2/works/{work_id}/announcements", params={"limit": 101})
    ).status_code == 422
    await client.patch(
        f"/v2/works/{work_id}/announcements/{first.json()['id']}",
        json={"body": "编辑不推送"},
    )
    await client.delete(f"/v2/works/{work_id}/announcements/{first.json()['id']}")
    principal.update(id="u2", discord_user_id="102")
    notices = (await client.get("/v2/me/notifications")).json()
    assert notices["total"] == 2
    assert any(item["body"] == "公告已撤回" for item in notices["items"])
    async with factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(Notification))
        ).scalar_one() == 2
        assert (
            await session.execute(select(func.count()).select_from(WorkTask))
        ).scalar_one() == before


async def test_v2_legacy_update_author_tag_review_and_shared_reads(
    client, factory, principal
):
    """旧更新映射公告去重，所有旧通知类型可读且沿用原行已读时间。"""
    work_id, _ = await seed_thread(factory, external=961)
    async with factory() as session:
        old = ThreadUpdate(
            thread_id=961,
            work_id=work_id,
            publisher_id=101,
            description="旧更新正文",
            source_message_at=utc_now(),
        )
        session.add(old)
        await session.flush()
        announcement = (
            await session.execute(
                select(WorkAnnouncement).where(
                    WorkAnnouncement.legacy_update_id == old.id
                )
            )
        ).scalar_one()
        announcement.withdrawn_at = utc_now()
        rows = [
            Notification(
                user_id=102,
                event_type="thread_update",
                event_source_id=old.id,
                thread_id=961,
            ),
            Notification(
                user_id=102,
                event_type="author_new_thread",
                event_source_id=961,
                thread_id=961,
            ),
            Notification(
                user_id=102,
                event_type="tag_review",
                event_source_id=7,
                target_type="booklist",
                target_id=42,
            ),
            Notification(
                user_id=102,
                event_type="work_announcement",
                event_source_id=announcement.id,
                event_source_platform="forum",
                work_id=work_id,
            ),
        ]
        session.add_all(rows)
        await session.commit()
        legacy_notice_id = rows[0].id
    principal.update(id="u2", discord_user_id="102")
    response = await client.get("/v2/me/notifications")
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 3 and page["unread_count"] == 3
    assert {item["type"] for item in page["items"]} == {
        "thread_update",
        "author_new_thread",
        "tag_review",
    }
    legacy = next(item for item in page["items"] if item["type"] == "thread_update")
    assert legacy["announcement_id"] == str(announcement.id) and legacy["withdrawn"]
    await client.post(f"/v2/me/notifications/{legacy_notice_id}/read")
    async with factory() as session:
        assert (await session.get(Notification, legacy_notice_id)).read_at is not None


async def test_v1_wire_filters_native_and_represents_forum_announcement(
    factory, monkeypatch
):
    """DC 可表达公告维持旧 wire，原生同雪花记录不进入列表总数和未读。"""
    from identity.runtime import request_identity_mode
    from api.v1.routers import notifications as router

    work_id, _ = await seed_thread(factory, external=962)
    native_work, _ = await seed_thread(factory, external=962, platform="fluxer")
    async with factory() as session:
        ann = WorkAnnouncement(
            work_id=work_id, author_id="u1", body="新公告", title="v2公告"
        )
        session.add(ann)
        await session.flush()
        session.add_all(
            [
                Notification(
                    user_id=102,
                    event_type="work_announcement",
                    event_source_id=ann.id,
                    event_source_platform="forum",
                    work_id=work_id,
                ),
                Notification(
                    user_id=102,
                    event_type="author_new_thread",
                    event_source_id=962,
                    event_source_platform="fluxer",
                    thread_id=962,
                    work_id=native_work,
                ),
            ]
        )
        await session.commit()
    monkeypatch.setattr(router, "AsyncSessionFactory", factory)
    marker = request_identity_mode.set("legacy")
    try:
        async with factory() as session:
            rows, total, unread = await NotificationRepository(session).list_for_user(
                102, False, 100, 0, legacy_visible=True
            )
            assert total == unread == len(rows) == 1
            assert (
                await NotificationRepository(session).unread_count(
                    102, legacy_visible=True
                )
                == 1
            )
        page = await router.list_notifications(
            limit=100, offset=0, unread_only=False, current_user={"id": "102"}
        )
        assert page.total == page.unread_count == len(page.results) == 1
        assert (
            page.results[0].type == "thread_update"
            and page.results[0].update.description == "新公告"
        )
        assert (
            page.results[0].thread.thread_id == "962"
            or str(page.results[0].thread.thread_id) == "962"
        )
    finally:
        request_identity_mode.reset(marker)


async def test_work_created_uses_source_time_and_platform_namespace(factory):
    """历史索引不通知后来关注者，平台同编号独立幂等且无 token 持久化。"""
    work_id, _ = await seed_thread(factory, external=963, platform="fluxer")
    created = utc_now() - timedelta(days=30)
    async with factory() as session:
        session.add_all(
            [
                User(id="u3", username="late"),
                DiscordIdentity(discord_user_id="103", user_id="u3"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                AuthorFollow(
                    user_id=102,
                    author_id=101,
                    fluxer_user_id="u2",
                    fluxer_author_id="u1",
                    followed_at=created - timedelta(days=1),
                ),
                AuthorFollow(
                    user_id=103,
                    author_id=101,
                    fluxer_user_id="u3",
                    fluxer_author_id="u1",
                    followed_at=created + timedelta(days=1),
                ),
                Notification(
                    user_id=102,
                    fluxer_user_id="u2",
                    event_type="author_new_thread",
                    event_source_id=963,
                    event_source_platform="discord",
                    thread_id=963,
                ),
            ]
        )
        await session.flush()
        service = NotificationFanoutService(session)
        assert (
            await service.fanout_work_created(work_id, 963, "u1", "fluxer", created)
            == 1
        )
        assert (
            await service.fanout_work_created(work_id, 963, "u1", "fluxer", created)
            == 0
        )
        rows = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.event_source_platform == "fluxer"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert (
            len(rows) == 1
            and rows[0].fluxer_user_id == "u2"
            and rows[0].created_at == created
        )
        await session.commit()


async def test_canonical_recipient_never_falls_back_to_rebound_discord(
    client, factory, principal
):
    """已投影给其他统一账号的历史通知不能因 DC 重新绑定而被读或标记。"""
    async with factory() as session:
        session.add(User(id="u3", username="previous"))
        await session.flush()
        notice = Notification(
            user_id=102,
            fluxer_user_id="u3",
            event_type="tag_review",
            event_source_id=77,
            target_type="booklist",
            target_id=42,
        )
        session.add(notice)
        await session.commit()
        identifier = notice.id
    principal.update(id="u2", discord_user_id="102")
    page = (await client.get("/v2/me/notifications")).json()
    assert page["total"] == page["unread_count"] == 0
    await client.post(f"/v2/me/notifications/{identifier}/read")
    async with factory() as session:
        assert (await session.get(Notification, identifier)).read_at is None


async def test_announcement_recipient_failure_rolls_back_publication(
    client, factory, monkeypatch
):
    """通知 SQL 失败时公告同时回滚，不存在公告成功但无受众通知的窗口。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    work_id, _ = await seed_thread(factory, external=964)
    async with factory() as session:
        session.add(
            ThreadFollow(
                work_id=work_id, thread_id=964, user_id=102, fluxer_user_id="u2"
            )
        )
        await session.commit()
    execute = AsyncSession.execute

    async def fail_notification(self, statement, *args, **kwargs):
        """保留真实数据库操作，仅在受众 SQL 插入时注入故障。"""
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "notification"
        ):
            raise RuntimeError("recipient persistence interrupted")
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_notification)
    with pytest.raises(RuntimeError, match="recipient persistence interrupted"):
        await client.post(
            f"/v2/works/{work_id}/announcements", json={"body": "必须原子发布"}
        )
    async with factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(WorkAnnouncement))
        ).scalar_one() == 0
        assert (
            await session.execute(select(func.count()).select_from(Notification))
        ).scalar_one() == 0


async def test_v1_canonical_only_and_hidden_v2_counts(
    factory, client, principal, monkeypatch
):
    """V1 兼容仅 canonical 接收者，V2 分类关闭同时过滤通知和计数。"""
    from identity.runtime import request_identity_mode
    from models.work import Work

    work_id, _ = await seed_thread(factory, external=965)
    async with factory() as session:
        session.add(
            Notification(
                fluxer_user_id="u2",
                event_type="author_new_thread",
                event_source_id=965,
                thread_id=965,
                work_id=work_id,
            )
        )
        await session.commit()
    marker = request_identity_mode.set("legacy")
    try:
        async with factory() as session:
            rows, total, unread = await NotificationRepository(session).list_for_user(
                102, False, 20, 0, legacy_visible=True
            )
            assert total == unread == len(rows) == 1
    finally:
        request_identity_mode.reset(marker)
    principal.update(id="u2", discord_user_id="102", show_nsfw=False)
    assert (await client.get("/v2/me/notifications")).json()["total"] == 1
    async with factory() as session:
        work = await session.get(Work, work_id)
        work.is_nsfw = True
        await session.commit()
    page = (await client.get("/v2/me/notifications")).json()
    assert page["total"] == page["unread_count"] == 0 and page["items"] == []


async def test_legacy_withdrawal_preserves_original_notice(client, factory, principal):
    """旧发布撤回后原通知仍显示已撤回，保留原 read_at 而不重新通知。"""
    work_id, _ = await seed_thread(factory, external=966)
    async with factory() as session:
        update = ThreadUpdate(
            thread_id=966,
            work_id=work_id,
            publisher_id=101,
            description="旧发布",
            source_message_at=utc_now(),
        )
        session.add(update)
        await session.flush()
        notice = Notification(
            user_id=102,
            event_type="thread_update",
            event_source_id=update.id,
            thread_id=966,
            read_at=utc_now(),
        )
        session.add(notice)
        await session.flush()
        identifier = notice.id
        await NotificationRepository(session).delete_for_event(
            "thread_update", update.id
        )
        await session.delete(update)
        await session.commit()
        assert await session.get(Notification, identifier) is not None
    principal.update(id="u2", discord_user_id="102")
    page = (await client.get("/v2/me/notifications")).json()
    assert page["total"] == 1 and page["items"][0]["withdrawn"]
    assert page["items"][0]["read_at"] is not None and page["unread_count"] == 0
