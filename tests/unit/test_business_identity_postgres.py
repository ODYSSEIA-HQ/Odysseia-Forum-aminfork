"""仅在显式指定的临时 PostgreSQL 和随机 schema 内验证业务身份。"""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from core.author_follow_repository import AuthorFollowRepository
from core.follow_repository import ThreadFollowRepository
from core.preferences_repository import PreferencesRepository
from core.notification_fanout_service import NotificationFanoutService
from core.notification_repository import NotificationRepository
from core.booklist_repository import BooklistRepository
from core.collection_repository import CollectionRepository
from models import (
    AuthorFollow,
    Booklist,
    BooklistItem,
    UserCollection,
    User,
    ThreadFollow,
    Notification,
    UserSearchPreferences,
)
from models.discord_identity import DiscordIdentity
from shared.enum import CollectionType


@pytest_asyncio.fixture
async def isolated_session():
    """在指定测试数据库中创建随机 schema，结束后只删除该 schema。"""
    url = os.environ.get("FLUXER_BUSINESS_TEST_URL")
    if not url:
        pytest.skip("需要显式指定临时 PostgreSQL FLUXER_BUSINESS_TEST_URL")
    schema = "test_business_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            for model in (
                User,
                DiscordIdentity,
                Booklist,
                BooklistItem,
                UserCollection,
                AuthorFollow,
                ThreadFollow,
                Notification,
                UserSearchPreferences,
            ):
                await conn.run_sync(model.__table__.create)
            await conn.execute(
                text(
                    "ALTER TABLE author_follow ALTER COLUMN user_id DROP NOT NULL, ALTER COLUMN author_id DROP NOT NULL"
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_test_fluxer_collection ON user_collection(fluxer_user_id,target_type,target_id)"
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_test_fluxer_follow ON author_follow(fluxer_user_id,fluxer_author_id)"
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_test_fluxer_notification ON notification(fluxer_user_id,event_type,event_source_id)"
                )
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_native_collections_and_follow_are_isolated(
    isolated_session, monkeypatch
):
    """原生用户和碰撞的 Discord 用户分别持有收藏与作者关注。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    session = isolated_session
    session.add_all(
        [
            User(id="100", username="native"),
            User(id="200", username="author"),
            DiscordIdentity(discord_user_id="100", user_id="200", version=1),
        ]
    )
    await session.commit()
    books = BooklistRepository(session)
    book = await books.create_booklist("100", "native")
    assert book.owner_id is None and book.fluxer_owner_id == "100"
    collections = CollectionRepository(session)
    assert await collections.add_collection(
        "100", CollectionType.BOOKLIST.value, book.id
    )
    assert await collections.get_collected_target_ids(
        "100", CollectionType.BOOKLIST, [book.id]
    ) == {book.id}
    assert (
        await collections.get_collected_target_ids(
            "200", CollectionType.BOOKLIST, [book.id]
        )
        == set()
    )
    relation = await AuthorFollowRepository(session).set_active("100", "200", True)
    await session.commit()
    assert relation.fluxer_user_id == "100" and relation.fluxer_author_id == "200"
    assert relation.user_id is None and relation.author_id == 100
    rows, total = await AuthorFollowRepository(session).list_for_user(
        "100", True, 10, 0
    )
    assert total == 1 and rows[0][1].id == "200"
    mapped_book = await books.get_or_create_default_booklist(100)
    assert mapped_book.fluxer_owner_id == "200"


@pytest.mark.asyncio
async def test_author_notification_fanout_uses_canonical_ids(
    isolated_session, monkeypatch
):
    """两个平台同一作者的关注均通过统一身份派发且重试幂等。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    session = isolated_session
    session.add_all(
        [
            User(id="100", username="reader"),
            User(id="200", username="author"),
            DiscordIdentity(discord_user_id="100", user_id="200", version=1),
        ]
    )
    await session.commit()
    await AuthorFollowRepository(session).set_active("100", "200", True)
    service = NotificationFanoutService(session)
    assert await service.fanout_author_new_thread(100, 321, 321) == 1
    assert await service.fanout_author_new_thread(100, 321, 321) == 0
    await session.commit()
    assert await NotificationRepository(session).unread_count("100") == 1
    assert await NotificationRepository(session).unread_count("200") == 0


@pytest.mark.asyncio
async def test_thread_follow_uses_canonical_and_maps_bot(isolated_session, monkeypatch):
    """帖子关注按统一身份隔离，机器人数值 ID 必须映射。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    session = isolated_session
    session.add_all(
        [
            User(id="100", username="reader"),
            User(id="200", username="author"),
            DiscordIdentity(discord_user_id="100", user_id="200", version=1),
        ]
    )
    await session.commit()
    follows = ThreadFollowRepository(session)
    assert await follows.add_follow("100", 321)
    assert await follows.add_follow(100, 321)
    assert await follows.is_following("100", 321)
    assert await follows.is_following("200", 321)
    assert await follows.remove_follow("100", 321)
    assert not await follows.is_following("100", 321)
    assert await follows.is_following("200", 321)
    with pytest.raises(ValueError):
        await follows.add_follow(999, 321)


@pytest.mark.asyncio
async def test_preferences_author_lists_use_unified_ids(isolated_session, monkeypatch):
    """新身份空间的作者偏好按字符串持久化并隔离数字相同的 Discord 用户。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    session = isolated_session
    session.add_all(
        [
            User(id="100", username="reader"),
            User(id="200", username="author"),
            DiscordIdentity(discord_user_id="100", user_id="200", version=1),
        ]
    )
    await session.commit()
    repo = PreferencesRepository(session)
    prefs = await repo.save_user_preferences("100", {"include_authors": ["200"]}, 1)
    assert prefs.user_id == "100" and prefs.include_authors == ["200"]
    assert await repo.get_user_preferences("200", 1) is None
    bot = await repo.save_user_preferences(100, {"exclude_authors": [100]}, 1)
    assert bot.user_id == "200" and bot.exclude_authors == ["200"]
    assert (await repo.get_user_preferences("100", 1)).include_authors == ["200"]


@pytest.mark.asyncio
async def test_legacy_follow_and_preferences_remain_discord_scoped(
    isolated_session, monkeypatch
):
    """默认旧模式继续按 Discord 数值读写关注和偏好。"""
    monkeypatch.delenv("FORUM_AUTH_MODE", raising=False)
    session = isolated_session
    follows = ThreadFollowRepository(session)
    assert await follows.add_follow(123, 321)
    assert await follows.is_following(123, 321)
    assert not await follows.is_following(456, 321)
    prefs = await PreferencesRepository(session).save_user_preferences(
        123, {"include_authors": [456]}, 1
    )
    assert prefs.user_id == 123 and prefs.include_authors == [456]
