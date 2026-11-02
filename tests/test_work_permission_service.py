"""成员角色短缓存、单飞和撤销边界。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import fakeredis.aioredis
import pytest
from fastapi import HTTPException
from work.work_permission_service import WorkPermissionService


@pytest.mark.asyncio
async def test_cache_singleflight_binding_epoch_and_sensitive_recheck():
    """并发普通读只查一次，绑定版本和撤销及写入都重新核验。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stored = {}
    cache = SimpleNamespace(
        get_member=AsyncMock(side_effect=lambda key: stored.get(key)),
        set_member=AsyncMock(
            side_effect=lambda key, value: stored.update({key: value})
        ),
    )
    verifier = SimpleNamespace(
        verify_member=AsyncMock(
            return_value=SimpleNamespace(
                outcome="verified", member={"roles": ["formal"]}
            )
        )
    )
    service = WorkPermissionService({}, redis=redis, cache=cache, verifier=verifier)
    try:
        await asyncio.gather(*(service.member("u1", "101", 1) for _ in range(12)))
        assert verifier.verify_member.await_count == 1
        await service.member("u1", "101", 2)
        assert verifier.verify_member.await_count == 2
        await redis.set("forum:fluxer:auth:v1:user_epoch:u1", "2")
        await service.member("u1", "101", 2)
        assert verifier.verify_member.await_count == 3
        await service.member("u1", "101", 2, sensitive=True)
        assert verifier.verify_member.await_count == 4
    finally:
        await service.close()
        await redis.aclose()


@pytest.mark.asyncio
async def test_inflight_revocation_does_not_refill_role_cache():
    """网络请求中发生撤销时拒绝将旧身份资格写回缓存。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = SimpleNamespace(
        get_member=AsyncMock(return_value=None), set_member=AsyncMock()
    )

    async def verify(*args, **kwargs):
        await redis.set("forum:fluxer:auth:v1:user_epoch:u1", "changed")
        return SimpleNamespace(outcome="verified", member={"roles": ["formal"]})

    service = WorkPermissionService(
        {},
        redis=redis,
        cache=cache,
        verifier=SimpleNamespace(verify_member=AsyncMock(side_effect=verify)),
    )
    try:
        with pytest.raises(HTTPException) as error:
            await service.member("u1", "101", 1)
        assert error.value.status_code == 503
        cache.set_member.assert_not_awaited()
    finally:
        await service.close()
        await redis.aclose()
