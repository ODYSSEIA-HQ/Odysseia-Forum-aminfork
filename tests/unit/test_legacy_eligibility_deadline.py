"""通过fakeredis的实际Lua解释器验证准入期限，无服务或真实凭据。"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi import HTTPException

from identity.fluxer_auth_service import FluxerAuthService


def service_with_redis(redis):
    """仅替换启动和权威传输，Lua、缓存逻辑及Redis过期由真实被测代码执行。"""
    service = FluxerAuthService(redis)
    service.PREFIX = "test:legacy-deadline:" + uuid.uuid4().hex
    service.ready = True
    service.start = AsyncMock()
    service.rpc = AsyncMock(return_value={"discord_user_id": "200", "user_id": "100", "identity_version": 3, "current": True, "eligible": True})
    return service


@pytest.mark.asyncio
async def test_actual_lua_positive_negative_deadlines_and_no_anchor_remainder():
    """普通成功与已知拒绝分别受三十秒、五秒上限约束。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        service = service_with_redis(redis)
        assert await service.legacy_eligibility("200", "100", 3)
        cache = [key async for key in redis.scan_iter(service.PREFIX + ":legacy:*")]
        assert len(cache) == 1 and 0 < await redis.pttl(cache[0]) <= 30000
        assert not [key async for key in redis.scan_iter(service.PREFIX + ":legacy-fill:*")]
        service.rpc.return_value = {"discord_user_id": "200", "user_id": None, "identity_version": None, "current": False, "eligible": False}
        assert not await service.legacy_eligibility("200", "100", 3, force_authority=True)
        assert 0 < await redis.pttl(cache[0]) <= 5000
        assert json.loads(await redis.get(cache[0]))["eligible"] is False
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible,left", [(True, 0), (False, 24000)])
async def test_actual_lua_rejects_queued_fill_after_original_server_budget(monkeypatch, eligible, left):
    """模拟SET排队到原始期限之后，必须不写cache且不返回过期权威结果。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = service_with_redis(redis)
    if not eligible:
        service.rpc.return_value = {"discord_user_id": "200", "user_id": None, "identity_version": None, "current": False, "eligible": False}
    original_eval = redis.eval
    async def queued(script, count, *arguments):
        if count == 4:
            anchor = arguments[3]
            await redis.pexpire(anchor, left)
        return await original_eval(script, count, *arguments)
    monkeypatch.setattr(redis, "eval", queued)
    try:
        with pytest.raises(HTTPException) as caught:
            await service.legacy_eligibility("200", "100", 3)
        assert caught.value.status_code == 503
        assert not [key async for key in redis.scan_iter(service.PREFIX + ":legacy:*")]
        assert not [key async for key in redis.scan_iter(service.PREFIX + ":legacy-fill:*")]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_late_write_ack_cannot_accept_current_request(monkeypatch):
    """服务端写入及时但响应迟到时，调用方的单调期限仍拒绝成功结果。"""
    import identity.fluxer_auth_service as module

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = service_with_redis(redis)
    clock = [1000.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original_eval = redis.eval
    async def late_ack(script, count, *arguments):
        answer = await original_eval(script, count, *arguments)
        if count == 4:
            clock[0] += 31
        return answer
    monkeypatch.setattr(redis, "eval", late_ack)
    try:
        with pytest.raises(HTTPException) as caught:
            await service.legacy_eligibility("200", "100", 3)
        assert caught.value.status_code == 503
    finally:
        await redis.aclose()
