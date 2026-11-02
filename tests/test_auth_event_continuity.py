"""验证认证消费者重建和保留缺口必定使正缓存失效。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from identity.auth_event_continuity import AuthEventContinuity


@pytest.fixture
def continuity():
    """只模拟 Redis 原子 CAS 与 NATS INFO，不访问默认数据库或消息代理。"""
    cache = {}
    consumer = SimpleNamespace(
        created="consumer-1", ack_floor=SimpleNamespace(stream_seq=4)
    )
    stream = SimpleNamespace(
        created="stream-1", state=SimpleNamespace(first_seq=1, last_seq=8)
    )

    async def evaluate(script, count, key, generation, previous, snapshot, reset):
        """保留生产 Lua 的比较与更新语义，用于观察原子代次变更。"""
        if cache.get(key, "") != previous:
            return 0
        if reset == "1":
            cache[generation] = cache.get(generation, 0) + 1
        cache[key] = snapshot
        return 1

    redis = SimpleNamespace(
        get=AsyncMock(side_effect=cache.get), eval=AsyncMock(side_effect=evaluate)
    )
    js = SimpleNamespace(
        consumer_info=AsyncMock(return_value=consumer),
        stream_info=AsyncMock(return_value=stream),
    )
    return AuthEventContinuity(redis), js, cache, consumer, stream


@pytest.mark.asyncio
async def test_startup_and_recreated_consumer_invalidate_without_core_disconnect(
    continuity,
):
    """Core 始终在线时，消费者代次变化也必须独立清除缓存。"""
    monitor, js, cache, consumer, stream = continuity
    assert await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth", startup=True)
    assert not await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    consumer.created = "consumer-2"
    assert await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    stream.created = "stream-2"
    assert await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    assert cache[monitor.prefix + ":generation"] == 3


@pytest.mark.asyncio
async def test_retention_gap_ack_rollback_and_truncation_reset_generation(continuity):
    """新丢失区间、ACK 回退和流截断不能复用既有正缓存。"""
    monitor, js, cache, consumer, stream = continuity
    await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    stream.state.first_seq = 7
    assert await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    assert not await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    consumer.ack_floor.stream_seq = 3
    assert await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    stream.state.last_seq = 6
    assert await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    assert cache[monitor.prefix + ":generation"] == 4


@pytest.mark.asyncio
async def test_info_or_redis_failure_is_not_reported_as_healthy(continuity):
    """依赖故障必须向调用方抛出，由消费者触发认证失败关闭。"""
    monitor, js, cache, consumer, stream = continuity
    js.stream_info.side_effect = RuntimeError("stream INFO denied")
    with pytest.raises(RuntimeError, match="INFO denied"):
        await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    assert not cache
    js.stream_info.side_effect = None
    monitor.redis.eval.side_effect = RuntimeError("redis unavailable")
    with pytest.raises(RuntimeError, match="redis unavailable"):
        await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")


@pytest.mark.asyncio
async def test_cursor_cas_conflict_refetches_authority_before_accepting(continuity):
    """并发检查不能用旧 INFO 覆盖另一个进程刚提交的较新基线。"""
    monitor, js, cache, consumer, stream = continuity
    original = monitor.redis.eval.side_effect
    calls = 0

    async def race(*args):
        """第一次模拟另一进程先写入相同基线，随后保留真实 CAS 结果。"""
        nonlocal calls
        calls += 1
        if calls == 1:
            cache[args[2]] = args[5]
            return 0
        return await original(*args)

    monitor.redis.eval.side_effect = race
    assert not await monitor.check(js, "ODYSSEIA_AUTH", "forum-auth")
    assert js.stream_info.await_count == 2
    saved = json.loads(cache[monitor.prefix + ":continuity:ODYSSEIA_AUTH:forum-auth"])
    assert saved["ack"] == 4
