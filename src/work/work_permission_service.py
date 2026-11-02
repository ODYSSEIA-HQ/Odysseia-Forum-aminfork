"""当前绑定的有界成员资格缓存与敏感实时核验。"""

import asyncio
import hashlib
import secrets
import time
import httpx
from fastapi import HTTPException
from core.discord_member_cache_service import DiscordMemberCacheService
from shared.redis_client import RedisManager


class WorkPermissionService:
    """共享连接池和固定锁分片，缓存失效与认证代次绑定。"""

    def __init__(self, config, redis=None, cache=None, verifier=None, client=None):
        """允许注入缓存和验证器以验证并发与撤销。"""
        self.config = config
        self.redis = redis
        self.cache = cache or DiscordMemberCacheService()
        self.verifier = verifier
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
        self.locks = [asyncio.Lock() for _ in range(256)]
        self.ttl = max(
            1,
            min(
                120,
                int(
                    config.get("work_permissions", {}).get("role_cache_ttl_seconds", 30)
                ),
            ),
        )

    async def close(self):
        """应用退出时关闭唯一共享 HTTP 连接池。"""
        await self.client.aclose()

    async def member(self, user_id, discord_id, binding_version, sensitive=False):
        """普通读复用短快照，敏感操作始终重新核验成员身份组。"""
        redis = self.redis or RedisManager.get_client()
        prefix = "forum:fluxer:auth:v1"
        generation, epoch = await redis.mget(
            prefix + ":generation", prefix + ":user_epoch:" + user_id
        )
        expected = {
            "forum_generation": str(generation or "0"),
            "forum_user_epoch": str(epoch or "0"),
            "forum_binding_version": binding_version,
        }

        async def valid_cache():
            """验证缓存时间、当前绑定及认证失效代次，不延长 TTL。"""
            cached = await self.cache.get_member(discord_id)
            if not cached or any(
                cached.get(key) != value for key, value in expected.items()
            ):
                return None
            verified = float(cached.get("roles_verified_at", 0))
            if not 0 <= time.time() - verified <= self.ttl:
                return None
            return cached

        if not sensitive:
            cached = await valid_cache()
            if cached is not None:
                return cached
        stripe = int.from_bytes(hashlib.sha256(discord_id.encode()).digest()[:2]) % len(
            self.locks
        )
        async with self.locks[stripe]:
            if not sensitive:
                cached = await valid_cache()
                if cached is not None:
                    return cached
            # Redis单飞覆盖多API实例；失败者等待成功快照，不回退长缓存。
            lock_key = "forum:v2:member-lock:" + discord_id
            token = secrets.token_hex(16)
            acquired = await redis.set(lock_key, token, nx=True, ex=15)
            if not acquired:
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    cached = await valid_cache()
                    if cached is not None and not sensitive:
                        return cached
                    if not await redis.get(lock_key):
                        break
                acquired = await redis.set(lock_key, token, nx=True, ex=15)
            if not acquired:
                raise HTTPException(503, "成员权限核验繁忙，请稍后重试")
            try:
                verifier = self.verifier
                if verifier is None:
                    from api.v1.routers.auth import _MEMBER_VERIFIER

                    verifier = _MEMBER_VERIFIER
                if verifier is None:
                    raise HTTPException(503, "成员权限验证器尚未初始化")
                result = await verifier.verify_member(
                    discord_id, secrets.token_hex(6), client=self.client
                )
                if result.outcome == "not_member":
                    await redis.delete("user:discord:" + discord_id)
                    raise HTTPException(403, "用户已不在 Discord 服务器")
                if result.outcome != "verified" or not isinstance(result.member, dict):
                    raise HTTPException(503, "当前成员权限暂不可用")
                member = {**result.member, **expected, "roles_verified_at": time.time()}
                # 若核验期间发生换绑/撤销，禁止以旧epoch回填授权。
                latest_generation, latest_epoch = await redis.mget(
                    prefix + ":generation", prefix + ":user_epoch:" + user_id
                )
                if (
                    str(latest_generation or "0") != expected["forum_generation"]
                    or str(latest_epoch or "0") != expected["forum_user_epoch"]
                ):
                    raise HTTPException(503, "成员权限状态已变化，请重试")
                await self.cache.set_member(discord_id, member)
                return member
            finally:
                await redis.eval(
                    "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                    1,
                    lock_key,
                    token,
                )
