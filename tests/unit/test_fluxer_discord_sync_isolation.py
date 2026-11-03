from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.v1.routers import fetch_images
from core.cache_service import CacheService
from core.sync_service import SyncService
from open_graph.reindex_consumer import OpenGraphReindexConsumer


@pytest.mark.asyncio
async def test_disabled_discord_sync_paths_make_no_requests_or_database_writes():
    bot = SimpleNamespace(
        api_scheduler=SimpleNamespace(submit=AsyncMock()),
        fetch_channel=AsyncMock(),
        get_partial_messageable=Mock(),
    )
    session_factory = Mock()
    service = SyncService(
        bot=bot,
        session_factory=session_factory,
        enable_discord_thread_sync=False,
    )

    results = [
        await service.sync_thread(123),
        await service.sync_thread(SimpleNamespace(id=123)),
        await service.sync_thread_channel(123),
        await service.sync_thread_messages(123),
    ]
    await service.pre_sync_forum_tags(456)

    assert [result.error_code for result in results] == [
        "discord_thread_sync_disabled",
        "discord_thread_sync_disabled",
        "discord_thread_sync_disabled",
        "discord_thread_sync_disabled",
    ]
    bot.api_scheduler.submit.assert_not_awaited()
    bot.fetch_channel.assert_not_awaited()
    bot.get_partial_messageable.assert_not_called()
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_open_graph_queue_fails_without_discord_fetch_or_database_write():
    bot = SimpleNamespace(
        api_scheduler=SimpleNamespace(submit=AsyncMock()),
        fetch_channel=AsyncMock(),
    )
    session_factory = Mock()
    service = SyncService(
        bot=bot,
        session_factory=session_factory,
        enable_discord_thread_sync=False,
    )
    queue = SimpleNamespace(
        finish_failure=AsyncMock(),
        finish_success=AsyncMock(),
    )
    consumer = OpenGraphReindexConsumer(service, queue)

    await consumer._process_job("job-123", 123)

    queue.finish_failure.assert_awaited_once_with(
        "job-123", 123, "discord_thread_sync_disabled"
    )
    queue.finish_success.assert_not_awaited()
    bot.api_scheduler.submit.assert_not_awaited()
    bot.fetch_channel.assert_not_awaited()
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_discord_image_endpoint_returns_before_request(monkeypatch):
    monkeypatch.setattr(fetch_images, "_discord_refresh_enabled", False)
    monkeypatch.setattr(fetch_images, "_async_session_factory", Mock())
    monkeypatch.setattr(fetch_images, "_bot_token", "test-token")
    process_item = AsyncMock()
    monkeypatch.setattr(fetch_images, "_process_single_item", process_item)

    payload = fetch_images.FetchImageRequest(
        items=[fetch_images.FetchImageItem(thread_id=123)]
    )
    with pytest.raises(fetch_images.HTTPException) as error:
        await fetch_images.refresh_thread_thumbnails(payload)

    assert error.value.status_code == 503
    process_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_channel_cache_refresh_skips_database_and_discord_io():
    bot = SimpleNamespace(
        api_scheduler=SimpleNamespace(submit=AsyncMock()),
        fetch_channel=AsyncMock(),
        get_channel=Mock(),
    )
    session_factory = Mock()
    service = CacheService(
        bot=bot,
        session_factory=session_factory,
        enable_discord_channel_refresh=False,
    )

    await service._refresh_indexed_channels_cache(prefer_gateway=True)

    session_factory.assert_not_called()
    bot.api_scheduler.submit.assert_not_awaited()
    bot.fetch_channel.assert_not_awaited()
    bot.get_channel.assert_not_called()
