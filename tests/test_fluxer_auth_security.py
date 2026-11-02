"""认证缓存的时间边界、错误分类和撤销竞态。"""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from identity.fluxer_auth_service import FluxerAuthService
from api.v1.routers import auth
from api.v1.dependencies import security


@pytest.fixture
def auth_service():
    """使用真实 Lua 解释器执行 Redis 脚本，只替换 NATS 的业务回复。"""
    import fakeredis
    import fakeredis.aioredis

    server = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    sync.set(FluxerAuthService.PREFIX + ":generation", "1")
    ttls = {}
    requests = []
    timeouts = []
    replies = [
        {
            "ok": True,
            "data": {
                "user_id": "200",
                "session_id_hash": "opaque_hash",
                "discord_user_id": "100",
                "username": "alice",
            },
        }
    ]
    hook = [None]

    async def evaluate(script, count, *args):
        result = await redis.eval(script, count, *args)
        if count == 2 and len(args[count:]) == 5 and result:
            ttls[args[1]] = int(args[count + 2])
        return result

    async def request(subject, payload, timeout):
        requests.append((subject, json.loads(payload)))
        timeouts.append(timeout)
        if hook[0]:
            await hook[0]()
        response = replies[0]
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(data=json.dumps(response).encode())

    async def subscribe(*args, **kwargs):
        return None

    async def flush(*args, **kwargs):
        return None

    async def close():
        connection.is_connected = False

    connection = SimpleNamespace(
        request=request,
        subscribe=subscribe,
        flush=flush,
        close=close,
        is_connected=True,
    )

    async def connector(**kwargs):
        connection.is_connected = True
        return connection

    adapter = SimpleNamespace(get=redis.get, incr=redis.incr, eval=evaluate)
    service = FluxerAuthService(adapter, connector=connector)
    service.connection = connection
    service.ready = True
    return SimpleNamespace(
        service=service,
        sync=sync,
        redis=redis,
        ttls=ttls,
        requests=requests,
        timeouts=timeouts,
        replies=replies,
        hook=hook,
    )


def invalidation(subject="events.auth.v1.session_invalidated", **fields):
    """构造精确主体的失效事件，不用全局代次伪造撤销。"""
    return SimpleNamespace(
        subject=subject,
        data=json.dumps(fields or {"session_id_hash": "opaque_hash"}).encode(),
    )


@pytest.mark.asyncio
async def test_positive_cache_hashes_token_without_renewal(auth_service):
    """缓存命中不刷新 TTL，也不把 token 放进键名。"""
    token = "flx_very-secret-value"
    first = await auth_service.service.resolve(token)
    assert first["user_id"] == "200"
    initial = dict(auth_service.ttls)
    assert await auth_service.service.resolve(token) == first
    assert len(auth_service.requests) == 1
    assert auth_service.ttls == initial
    assert all(token not in key for key in auth_service.sync.keys())
    assert all(0 < ttl <= 3600 for ttl in auth_service.ttls.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["INVALID_TOKEN", "ACCOUNT_UNAVAILABLE"])
async def test_only_definitive_invalid_tokens_receive_five_second_cache(
    auth_service, code
):
    """明确无效结果才短暂负缓存。"""
    auth_service.replies[0] = {"ok": False, "error": {"code": code, "retryable": False}}
    assert await auth_service.service.resolve("flx_invalid") is None
    assert await auth_service.service.resolve("flx_invalid") is None
    assert len(auth_service.requests) == 1
    assert all(0 < ttl <= 5 for ttl in auth_service.ttls.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        TimeoutError(),
        {"ok": False, "error": {"code": "UNAVAILABLE", "retryable": True}},
        {"bad": "response"},
    ],
)
async def test_dependency_failure_is_503_and_not_negative_cached(
    auth_service, response
):
    """超时、依赖错误和畸形响应不会伪装成用户凭证无效。"""
    auth_service.replies[0] = response
    with pytest.raises(HTTPException) as error:
        await auth_service.service.resolve("flx_valid")
    assert error.value.status_code == 503
    assert not auth_service.ttls


@pytest.mark.asyncio
async def test_inflight_resolve_cannot_repopulate_after_invalidation(auth_service):
    """RPC 飞行期间的撤销事件阻止旧结果写回与返回。"""
    auth_service.hook[0] = lambda: auth_service.service.invalidated(invalidation())
    with pytest.raises(HTTPException) as error:
        await auth_service.service.resolve("flx_racing")
    assert error.value.status_code == 503
    assert not auth_service.ttls


@pytest.mark.asyncio
async def test_disconnect_refuses_cached_tokens_and_reconnect_flushes(auth_service):
    """断线不接受旧缓存，重连会强制重新查权威会话。"""
    await auth_service.service.resolve("flx_live")
    await auth_service.service.disconnected()
    with pytest.raises(HTTPException) as error:
        await auth_service.service.resolve("flx_live")
    assert error.value.status_code == 503
    await auth_service.service.reconnected()
    await auth_service.service.resolve("flx_live")
    assert len(auth_service.requests) == 2


@pytest.mark.asyncio
async def test_miss_concurrency_has_bounded_wait(auth_service):
    """所有外部请求槽占用时快速返回服务繁忙。"""
    for _ in range(64):
        await auth_service.service.misses.acquire()
    with pytest.raises(HTTPException) as error:
        await auth_service.service.resolve("flx_overload")
    assert error.value.status_code == 503
    assert not auth_service.requests


def request(path, headers=(), cookie=None):
    """构造真实 HTTP 请求作用域，Cookie 与 Bearer 明确区分。"""
    raw = [(name.lower().encode(), value.encode()) for name, value in headers]
    if cookie:
        raw.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "method": "POST", "path": path, "headers": raw})


@pytest.mark.asyncio
async def test_fluxer_security_ignores_cookie_and_uses_authoritative_principal(
    monkeypatch,
):
    """Fluxer 不接受可被跨站自动附带的旧 session Cookie。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    called = []

    async def resolve(token):
        called.append(token)
        return {"id": "200", "discord_user_id": None}

    monkeypatch.setattr(security, "resolve_principal", resolve)
    assert (
        await security.get_current_user(
            request("/v1/test", cookie="session=flx_cookie"), None
        )
        is None
    )
    principal = await security.get_current_user(
        request("/v1/test", [("Authorization", "Bearer flx_valid")]), None
    )
    assert principal["id"] == "200"
    assert principal["discord_user_id"] is None
    assert called == ["flx_valid"]


@pytest.mark.asyncio
async def test_claim_is_one_use_and_bound_to_browser_and_origin(
    monkeypatch, auth_service
):
    """泄露登录码不足以跨浏览器或跨来源领取会话。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    monkeypatch.setattr(auth, "_AUTH_CONFIG", {"frontend_url": "https://forum.example"})
    monkeypatch.setattr(
        auth.RedisManager, "get_client", lambda: auth_service.service.redis
    )
    key = "forum:login:claim:" + hashlib.sha256(b"code").hexdigest()
    auth_service.sync.set(
        key,
        json.dumps(
            {
                "binding": hashlib.sha256(b"browser").hexdigest(),
                "token": "flx_secret",
                "user": {"id": "200"},
            }
        ),
    )
    for origin, cookie in [
        ("https://evil.example", "browser"),
        ("https://forum.example", "other-browser"),
    ]:
        with pytest.raises(HTTPException):
            await auth.claim_session(
                request(
                    "/v1/auth/session/claim",
                    [("Origin", origin)],
                    "forum_login_binding=" + cookie,
                ),
                {"login_code": "code"},
            )
        assert auth_service.sync.exists(key)
    valid = request(
        "/v1/auth/session/claim",
        [("Origin", "https://forum.example")],
        "forum_login_binding=browser",
    )
    response = await auth.claim_session(valid, {"login_code": "code"})
    assert json.loads(response.body)["token"] == "flx_secret"
    assert response.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException):
        await auth.claim_session(valid, {"login_code": "code"})


@pytest.mark.asyncio
async def test_callback_rejects_missing_or_cross_browser_state(
    monkeypatch, auth_service
):
    """Discord OAuth 回调在读取授权码前校验 state 与浏览器绑定。"""
    monkeypatch.setattr(auth, "_AUTH_CONFIG", {"frontend_url": "https://forum.example"})
    monkeypatch.setattr(
        auth.RedisManager, "get_client", lambda: auth_service.service.redis
    )
    state_key = "forum:oauth:state:" + hashlib.sha256(b"state").hexdigest()
    auth_service.sync.set(state_key, hashlib.sha256(b"browser").hexdigest())
    result = await auth.callback(
        "code",
        request("/v1/auth/callback", cookie="forum_login_binding=attacker"),
        "state",
    )
    assert "status=400" in result.headers["location"]
    assert auth_service.sync.exists(state_key)
    result = await auth.callback("code", request("/v1/auth/callback"), None)
    assert "status=400" in result.headers["location"]


@pytest.mark.asyncio
async def test_positive_ttl_starts_before_authoritative_request(
    monkeypatch, auth_service
):
    """外部请求耗时计入缓存寿命，而不是从 RPC 完成后重新开始。"""
    from identity import fluxer_auth_service

    now = [1000.0]
    monkeypatch.setattr(fluxer_auth_service.time, "time", lambda: now[0])

    async def slow_resolve():
        now[0] += 120.0

    auth_service.hook[0] = slow_resolve
    await auth_service.service.resolve("flx_slow_valid")
    assert list(auth_service.ttls.values()) == [3480]


@pytest.mark.asyncio
async def test_session_invalidation_preserves_other_sessions_and_users(auth_service):
    """单会话撤销不会冲掉同用户其他会话或四十万其他用户缓存。"""
    service = auth_service.service
    await service.resolve("flx_alice_one")
    auth_service.replies[0]["data"]["session_id_hash"] = "alice_two"
    await service.resolve("flx_alice_two")
    auth_service.replies[0]["data"].update(user_id="300", session_id_hash="bob_one")
    await service.resolve("flx_bob")
    generation = auth_service.sync.get(service.PREFIX + ":generation")
    await service.invalidated(
        invalidation(session_id_hash="opaque_hash", user_id="200")
    )
    assert auth_service.sync.get(service.PREFIX + ":generation") == generation
    await service.resolve("flx_alice_two")
    await service.resolve("flx_bob")
    assert len(auth_service.requests) == 3
    auth_service.replies[0]["data"].update(user_id="200", session_id_hash="opaque_hash")
    await service.resolve("flx_alice_one")
    assert len(auth_service.requests) == 4


@pytest.mark.asyncio
async def test_user_epoch_invalidates_only_that_users_sessions(auth_service):
    """账户撤销覆盖该账户全部会话，但保留其他账户的正缓存。"""
    service = auth_service.service
    await service.resolve("flx_alice_one")
    auth_service.replies[0]["data"]["session_id_hash"] = "alice_two"
    await service.resolve("flx_alice_two")
    auth_service.replies[0]["data"].update(user_id="300", session_id_hash="bob_one")
    await service.resolve("flx_bob")
    await service.invalidated(
        invalidation("events.auth.v1.user_invalidated", user_id="200")
    )
    await service.resolve("flx_bob")
    assert len(auth_service.requests) == 3
    auth_service.replies[0]["data"].update(user_id="200", session_id_hash="opaque_hash")
    await service.resolve("flx_alice_one")
    auth_service.replies[0]["data"]["session_id_hash"] = "alice_two"
    await service.resolve("flx_alice_two")
    assert len(auth_service.requests) == 5


@pytest.mark.asyncio
async def test_inflight_unknown_user_revocation_blocks_only_target(auth_service):
    """RPC 开始前未知 user_id，仍能阻止中途目标撤销；无关事件不误拒绝。"""
    service = auth_service.service
    auth_service.hook[0] = lambda: service.invalidated(
        invalidation("events.auth.v1.user_invalidated", user_id="200")
    )
    with pytest.raises(HTTPException) as error:
        await service.resolve("flx_racing_user")
    assert error.value.status_code == 503
    assert not auth_service.ttls
    auth_service.hook[0] = lambda: service.invalidated(
        invalidation("events.auth.v1.user_invalidated", user_id="999")
    )
    assert (await service.resolve("flx_unrelated_event"))["user_id"] == "200"


@pytest.mark.asyncio
async def test_invalidation_failure_recovers_without_actual_broker_reconnect(
    auth_service, monkeypatch
):
    """Redis 短暂故障后关闭旧连接，恢复时可重新 start，不会永久 503。"""
    service = auth_service.service
    original = service.redis.eval

    async def unavailable(*args, **kwargs):
        raise ConnectionError("isolated redis outage")

    monkeypatch.setattr(service.redis, "eval", unavailable)
    await service.invalidated(invalidation())
    assert not service.ready
    await service.reset_task
    assert service.connection is None
    monkeypatch.setattr(service.redis, "eval", original)
    assert (await service.resolve("flx_recovered"))["user_id"] == "200"
    assert service.ready
    await service.close()


@pytest.mark.asyncio
async def test_binding_failure_retained_and_retried_before_ready(
    auth_service, monkeypatch
):
    """绑定投影失败时保留事件，重建连接前完成用户与绑定一致性修复。"""
    service = auth_service.service
    applied = []

    async def failing(event):
        raise ConnectionError("isolated projection outage")

    monkeypatch.setattr(service, "apply_binding", failing)
    event = invalidation(
        "events.identity.v1.binding_changed",
        user_id="200",
        discord_user_id="100",
        version=3,
    )
    await service.invalidated(event)
    await service.reset_task
    assert service.pending_bindings["100"]["version"] == 3

    async def repaired(event):
        applied.append((event["discord_user_id"], event["user_id"], event["version"]))

    monkeypatch.setattr(service, "apply_binding", repaired)
    await service.start()
    assert applied == [("100", "200", 3)]
    assert not service.pending_bindings
    assert service.ready
    await service.close()


@pytest.mark.asyncio
async def test_exchange_has_long_bounded_timeout_but_resolve_stays_short(auth_service):
    """首次开户头像处理不会被固定五秒截断，普通认证仍保持短超时。"""
    service = auth_service.service
    await service.rpc(
        "rpc.auth.v1.external.exchange", exchange_id="safe_test_operation"
    )
    await service.resolve("flx_bounded_resolve")
    assert auth_service.timeouts == [25, 5]
