"""验证 Discord 原始 gateway 删除入口无需频道缓存。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ThreadManager.cog import ThreadManager


@pytest.mark.asyncio
async def test_uncached_starter_delete_is_authoritative():
    """原始首楼删除携带真实服务器范围，不需要调用 REST 取证。"""
    cog = SimpleNamespace(logic=SimpleNamespace(delete_thread_permanently=AsyncMock()))
    payload = SimpleNamespace(guild_id=999, channel_id=123, message_id=123)
    await ThreadManager.on_raw_message_delete(cog, payload)
    cog.logic.delete_thread_permanently.assert_awaited_once_with(123, 999)


@pytest.mark.asyncio
async def test_uncached_thread_delete_is_scoped_to_indexed_parent():
    """原始帖子删除只接受已索引父论坛并传递服务器范围。"""
    logic = SimpleNamespace(delete_thread_permanently=AsyncMock())
    cog = SimpleNamespace(logic=logic, is_channel_indexed=lambda parent: parent == 200)
    payload = SimpleNamespace(guild_id=999, parent_id=200, thread_id=123)
    await ThreadManager.on_raw_thread_delete(cog, payload)
    logic.delete_thread_permanently.assert_awaited_once_with(123, 999)
    payload.parent_id = 201
    await ThreadManager.on_raw_thread_delete(cog, payload)
    assert logic.delete_thread_permanently.await_count == 1


@pytest.mark.asyncio
async def test_dm_delete_never_changes_work():
    """没有服务器来源的删除不进入作品同步。"""
    cog = SimpleNamespace(logic=SimpleNamespace(delete_thread_permanently=AsyncMock()))
    await ThreadManager.on_raw_message_delete(
        cog, SimpleNamespace(guild_id=None, channel_id=123, message_id=123)
    )
    cog.logic.delete_thread_permanently.assert_not_awaited()
