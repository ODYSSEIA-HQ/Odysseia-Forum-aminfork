# ruff: noqa: F811  （从别的测试模块导入的 fixture 被当成参数名重定义）
"""/api/v1 限频（协议 1.6，契约 RL-01 / RL-02）：按账号的写请求、按 IP 的未登录请求，429 + Retry-After。"""

import httpx
import pytest
import pytest_asyncio
from starlette.datastructures import Headers

from site_v1 import rate_limit
from site_v1.config import reload_config

from test_site_v1_session_api import BASE, Client, assert_error, env  # noqa: F401


@pytest_asyncio.fixture
async def limited(env, monkeypatch, redis_client):  # noqa: F811
    monkeypatch.setenv("SITE_RL_ENABLED", "1")
    monkeypatch.setenv("SITE_RL_ACCOUNT_WRITE_LIMIT", "5")
    monkeypatch.setenv("SITE_RL_IP_WRITE_LIMIT", "4")
    monkeypatch.setenv("SITE_RL_IP_LIMIT", "50")
    reload_config()
    await rate_limit.reset(redis_client)
    yield env
    await rate_limit.reset(redis_client)


@pytest.mark.asyncio
async def test_account_write_limit(limited):
    app, account, _ = limited
    await account("rl_user")
    c = Client(app)
    assert (await c.login("rl_user")).status_code == 200
    for _ in range(5):
        res = await c.write("PATCH", "/me/prefs", {"blurR18": True})
        assert res.status_code == 200, res.text
    res = await c.write("PATCH", "/me/prefs", {"blurR18": True})
    err = assert_error(res, 429, "rate_limited")
    assert err["details"]["scope"] == "account"
    retry = int(res.headers["Retry-After"])
    assert 1 <= retry <= 60
    assert res.headers["Cache-Control"] == "no-store"
    # 读请求不算写请求的账
    assert (await c.get("/me")).status_code == 200
    # 另一个账号不受影响
    await account("rl_other")
    other = Client(app)
    await other.login("rl_other")
    assert (
        await other.write("PATCH", "/me/prefs", {"blurR18": True})
    ).status_code == 200
    await c.close()
    await other.close()


@pytest.mark.asyncio
async def test_anonymous_ip_limit_on_failed_logins(limited):
    app, _, _ = limited
    c = Client(app)
    await c.session()
    statuses = []
    for _ in range(6):
        res = await c.write(
            "POST", "/auth/login", {"username": "nobody", "password": "x"}
        )
        statuses.append(res.status_code)
        if res.status_code == 429:
            break
    assert statuses[-1] == 429 and 429 not in statuses[:-1], statuses
    err = assert_error(res, 429, "rate_limited")
    assert err["details"]["scope"] == "ip"
    assert int(res.headers["Retry-After"]) >= 1
    # 同一 IP 的其他未登录写请求也被限，读请求按读的桶（阈值大）照常
    assert (
        await c.write(
            "POST", "/auth/register", {"username": "x1", "password": "pw123456"}
        )
    ).status_code == 429
    assert (await c.get("/site")).status_code == 200
    await c.close()


@pytest.mark.asyncio
async def test_disabled_never_limits(env, monkeypatch, redis_client):  # noqa: F811
    monkeypatch.setenv("SITE_RL_ENABLED", "0")
    monkeypatch.setenv("SITE_RL_IP_WRITE_LIMIT", "1")
    reload_config()
    app, _, _ = env
    c = Client(app)
    await c.session()
    for _ in range(3):
        res = await c.write(
            "POST", "/auth/login", {"username": "nobody", "password": "x"}
        )
        assert res.status_code != 429
    await c.close()


@pytest.mark.asyncio
async def test_old_routes_not_limited(limited):
    """/api/v1 以外的路径不经过这里的限频。"""
    app, _, _ = limited

    @app.post("/plain/echo")
    async def echo():
        return {"ok": True}

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    for _ in range(10):
        assert (await http.post("/plain/echo")).status_code == 200
    await http.aclose()


def test_client_ip_rules(monkeypatch):
    scope = {"client": ("10.0.0.5", 1234)}
    xff = Headers(
        {"x-forwarded-for": "6.6.6.6, 1.2.3.4, 10.0.0.9", "cf-connecting-ip": "9.9.9.9"}
    )

    monkeypatch.delenv("SITE_RL_TRUST_CF", raising=False)
    monkeypatch.delenv("SITE_RL_TRUSTED_PROXIES", raising=False)
    reload_config()
    # 默认：只看直连地址，不信任任何头
    assert rate_limit.client_ip(scope, xff) == "10.0.0.5"

    monkeypatch.setenv("SITE_RL_TRUSTED_PROXIES", "10.0.0.0/8")
    reload_config()
    # 受信代理链：从右往左跳过受信代理，取最右边的不可信地址（不是第一个值 6.6.6.6）
    assert rate_limit.client_ip(scope, xff) == "1.2.3.4"
    # 直连地址不是受信代理时不看 X-Forwarded-For
    assert rate_limit.client_ip({"client": ("5.5.5.5", 1)}, xff) == "5.5.5.5"

    monkeypatch.setenv("SITE_RL_TRUST_CF", "1")
    reload_config()
    assert rate_limit.client_ip(scope, xff) == "9.9.9.9"
    monkeypatch.delenv("SITE_RL_TRUST_CF")
    monkeypatch.delenv("SITE_RL_TRUSTED_PROXIES")
    reload_config()
