"""/api/v1 打底和账号会话部分：会话 Cookie、CSRF、登录注册、偏好和 R18 资格、用户关系。

用 FakeFluxer + create_all 的测试库（TEST_DB_URL），不需要 NATS。
每个用例新建一个只挂 /api/v1 的 FastAPI app（同样的 install），避免依赖旧接口的启动配置。
"""

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from models.author_follow import AuthorFollow
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserBlock
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.services.accounts import ensure_account_rows
from shared.time_utils import utc_now

BASE = "https://site.test"
PASSWORD = "demo1234"


@pytest_asyncio.fixture
async def env(monkeypatch, db_session_factory, redis_client):
    monkeypatch.setenv("FLUXER_FAKE", "1")
    monkeypatch.setenv("SITE_REGISTRATION_MODE", "open")
    monkeypatch.setenv("SITE_TEST_HOOKS", "1")
    monkeypatch.delenv("SITE_COOKIE_SECURE", raising=False)
    reload_config()
    set_session_factory(db_session_factory)
    fake = FakeFluxer(session_factory=db_session_factory, redis=redis_client)
    set_fluxer(fake)

    from site_v1 import install

    app = FastAPI()
    install(app)

    @app.get("/api/v1/__boom")
    async def boom():
        raise RuntimeError("未处理的异常")

    @app.get("/plain/missing-validation")
    async def plain(limit: int):
        return {"limit": limit}

    async def account(username, *, access="none", newcomer=False, permissions=()):
        user_id = await fake.ensure_account(
            username, PASSWORD, email=f"{username}@example.com"
        )
        async with db_session_factory() as session:
            await ensure_account_rows(session, user_id, newcomer=newcomer)
            row = await session.get(SiteRestrictedAccess, user_id)
            row.state = access
            if access == "granted":
                row.age_confirmed_at = utc_now()
                row.terms_version = "2026-10"
            acc = await session.get(SiteAccount, user_id)
            acc.permissions = list(permissions)
            await session.commit()
        return user_id

    yield app, account, db_session_factory
    set_session_factory(None)
    set_fluxer(None)
    for var in ("FLUXER_FAKE", "SITE_REGISTRATION_MODE", "SITE_TEST_HOOKS"):
        monkeypatch.delenv(var, raising=False)
    reload_config()


class Client:
    """带 Cookie 和 CSRF 的测试客户端（和契约测试的 client 一样的用法）。"""

    def __init__(self, app):
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        )
        self.csrf = None

    async def session(self):
        res = await self.http.get("/api/v1/session")
        self.csrf = res.json()["csrfToken"]
        return res

    async def write(self, method, path, json=None, csrf=True):
        if csrf and self.csrf is None:
            await self.session()
        headers = (
            {"X-CSRF-Token": self.csrf}
            if csrf is True
            else ({"X-CSRF-Token": csrf} if csrf else {})
        )
        return await self.http.request(
            method, "/api/v1" + path, json=json, headers=headers
        )

    async def get(self, path, **kw):
        return await self.http.get("/api/v1" + path, **kw)

    async def login(self, username, password=PASSWORD):
        await self.session()
        res = await self.write(
            "POST", "/auth/login", {"username": username, "password": password}
        )
        await self.session()
        return res

    async def close(self):
        await self.http.aclose()


def assert_error(res, status, code):
    assert res.status_code == status, res.text
    assert res.headers["X-Content-Mode"] in ("general", "r18")
    body = res.json()
    assert body["error"]["code"] == code and body["error"]["message"]
    return body["error"]


@pytest.mark.asyncio
async def test_site_and_anonymous_session(env):
    app, _, _ = env
    c = Client(app)
    site = await c.get("/site")
    assert site.status_code == 200 and site.headers["X-Content-Mode"] == "general"
    body = site.json()
    assert body["termsVersion"] == "2026-10"
    # 协议 0.5：features 是字符串数组
    assert "registration" in body["features"] and body["registrationMode"] == "open"
    assert all(isinstance(v, str) for v in body["features"])
    assert "presence" in body["features"]

    res = await c.session()
    data = res.json()
    assert data == {"authenticated": False, "csrfToken": data["csrfToken"], "me": None}
    cookie = res.headers["set-cookie"]
    for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attr in cookie
    # 同一 Cookie 再取 /session：csrf 不变，不再下发 Cookie
    again = await c.get("/session")
    assert (
        again.json()["csrfToken"] == data["csrfToken"]
        and "set-cookie" not in again.headers
    )
    await c.close()


@pytest.mark.asyncio
async def test_csrf_required_on_every_write(env):
    app, account, _ = env
    await account("plain")
    c = Client(app)
    await c.session()
    assert_error(
        await c.write(
            "POST",
            "/auth/login",
            {"username": "plain", "password": PASSWORD},
            csrf=False,
        ),
        403,
        "forbidden",
    )
    assert_error(
        await c.write(
            "POST",
            "/auth/login",
            {"username": "plain", "password": PASSWORD},
            csrf="wrong",
        ),
        403,
        "forbidden",
    )
    # 没有会话 Cookie 的写请求也是 403
    fresh = Client(app)
    assert_error(
        await fresh.write("POST", "/auth/logout", csrf="anything"), 403, "forbidden"
    )
    await c.close()
    await fresh.close()


@pytest.mark.asyncio
async def test_login_rotates_session_and_logout(env):
    app, account, _ = env
    user_id = await account("plain", newcomer=True)
    c = Client(app)
    await c.session()
    old_csrf = c.csrf
    old_sid = c.http.cookies.get("odysseia_sid")

    err = assert_error(
        await c.write(
            "POST", "/auth/login", {"username": "plain", "password": "wrong"}
        ),
        422,
        "validation_failed",
    )
    assert "password" in err["details"]["fields"]

    res = await c.write(
        "POST", "/auth/login", {"username": "PLAIN@example.com", "password": PASSWORD}
    )
    assert res.status_code == 200, res.text
    me = res.json()
    assert me["id"] == user_id and me["username"] == "plain"
    assert me["isNewcomer"] is True and me["onboarding"]["newcomerUntil"].endswith("Z")
    assert (
        me["restrictedAccess"]["state"] == "none"
        and me["prefs"]["contentMode"] == "general"
    )
    assert "flx_" not in res.text and "flx_" not in res.headers.get("set-cookie", "")
    assert c.http.cookies.get("odysseia_sid") != old_sid

    session = (await c.session()).json()
    assert session["authenticated"] and session["me"]["id"] == user_id
    assert c.csrf != old_csrf
    # 旧 csrf 失效
    assert_error(
        await c.write("PATCH", "/me/prefs", {"blurR18": False}, csrf=old_csrf),
        403,
        "forbidden",
    )

    out = await c.write("POST", "/auth/logout")
    assert out.status_code == 204 and "Max-Age=0" in out.headers["set-cookie"]
    assert_error(await c.get("/me"), 401, "unauthenticated")
    assert (await c.session()).json()["authenticated"] is False
    await c.close()


@pytest.mark.asyncio
async def test_register_open_and_discord_only(env, monkeypatch):
    app, account, factory = env
    await account("taken")
    c = Client(app)
    err = assert_error(
        await c.write("POST", "/auth/register", {"username": "a", "password": "1"}),
        422,
        "validation_failed",
    )
    assert {"username", "password"} <= set(err["details"]["fields"])
    err = assert_error(
        await c.write(
            "POST", "/auth/register", {"username": "taken", "password": "longenough"}
        ),
        422,
        "validation_failed",
    )
    assert "username" in err["details"]["fields"]

    res = await c.write(
        "POST",
        "/auth/register",
        {"username": "newcomer_1", "password": "longenough", "displayName": "新人"},
    )
    assert res.status_code == 201, res.text
    me = res.json()
    assert me["displayName"] == "新人" and me["isNewcomer"] is True
    assert (
        me["restrictedAccess"]["state"] == "none"
        and me["prefs"]["contentMode"] == "general"
    )
    assert (await c.session()).json()["authenticated"] is True

    monkeypatch.setenv("SITE_REGISTRATION_MODE", "discord_only")
    reload_config()
    other = Client(app)
    err = assert_error(
        await other.write(
            "POST", "/auth/register", {"username": "someone", "password": "longenough"}
        ),
        403,
        "forbidden",
    )
    assert err["details"]["reason"] == "registration_discord_only"
    site = (await other.get("/site")).json()
    assert "registrationDiscordOnly" in site["features"]
    assert "registration" not in site["features"]
    await c.close()
    await other.close()


@pytest.mark.asyncio
async def test_prefs_and_restricted_access_take_effect_immediately(env):
    app, account, _ = env
    await account("plain")
    await account("r18user", access="granted")
    plain, r18 = Client(app), Client(app)
    await plain.login("plain")
    await r18.login("r18user")

    err = assert_error(
        await plain.write("PATCH", "/me/prefs", {"contentMode": "nonsense"}),
        422,
        "validation_failed",
    )
    assert "contentMode" in err["details"]["fields"]
    status = (await plain.get("/me/restricted-access")).json()
    assert status["termsVersion"] == "2026-10" and any(
        not c["met"] for c in status["conditions"]
    )
    err = assert_error(
        await plain.write("PATCH", "/me/prefs", {"contentMode": "r18"}),
        403,
        "restricted_access_required",
    )
    ids = {c["id"] for c in status["conditions"]} | {"revoked"}
    assert err["details"]["unmet"] and set(err["details"]["unmet"]) <= ids

    res = await r18.write(
        "PATCH",
        "/me/prefs",
        {"contentMode": "r18", "showR18G": True, "pinnedBoards": ["b1", "b1", "b2"]},
    )
    assert res.status_code == 200 and res.headers["X-Content-Mode"] == "r18"
    assert res.json()["prefs"]["pinnedBoards"] == ["b1", "b2"]
    assert (await r18.get("/me")).headers["X-Content-Mode"] == "r18"

    # 测试钩子撤销资格：同一会话下一个请求就回到 general
    hook = await r18.http.post(
        "/__test__/users/r18user/restricted-access", json={"state": "revoked"}
    )
    assert hook.status_code == 204
    me = await r18.get("/me")
    assert (
        me.headers["X-Content-Mode"] == "general"
        and me.json()["restrictedAccess"]["state"] == "revoked"
    )
    err = assert_error(
        await r18.write("PATCH", "/me/prefs", {"contentMode": "r18"}),
        403,
        "restricted_access_required",
    )
    assert "revoked" in err["details"]["unmet"]
    # 撤销后确认条款不恢复资格
    await r18.write(
        "POST", "/me/restricted-access/confirm", {"termsVersion": "2026-10"}
    )
    assert (await r18.get("/me")).json()["restrictedAccess"]["state"] == "revoked"
    await r18.http.post(
        "/__test__/users/r18user/restricted-access", json={"state": "granted"}
    )
    assert (await r18.get("/me")).headers["X-Content-Mode"] == "r18"

    # 确认条款：版本不对 422；对了且条件满足（fake 模式只有 age_confirm）就授予
    assert_error(
        await plain.write(
            "POST", "/me/restricted-access/confirm", {"termsVersion": "x"}
        ),
        422,
        "validation_failed",
    )
    granted = await plain.write(
        "POST", "/me/restricted-access/confirm", {"termsVersion": "2026-10"}
    )
    assert granted.json()["restrictedAccess"]["state"] == "granted"
    assert (
        await plain.write("PATCH", "/me/prefs", {"contentMode": "r18"})
    ).status_code == 200
    await plain.close()
    await r18.close()


@pytest.mark.asyncio
async def test_patch_me_display_name(env):
    app, account, _ = env
    await account("plain")
    c = Client(app)
    await c.login("plain")
    assert_error(
        await c.write("PATCH", "/me", {"displayName": ""}), 422, "validation_failed"
    )
    res = await c.write("PATCH", "/me", {"displayName": "  新名字 "})
    assert res.status_code == 200 and res.json()["displayName"] == "新名字"
    assert (await c.session()).json()["me"]["displayName"] == "新名字"
    await c.close()


@pytest.mark.asyncio
async def test_users_profile_follow_block(env):
    app, account, factory = env
    me_id = await account("plain")
    author_id = await account("author", access="granted")
    anon = Client(app)
    profile = (await anon.get(f"/users/{author_id}")).json()
    assert (
        profile["id"] == author_id
        and "email" not in profile
        and "viewer" not in profile
    )
    assert profile["createdAt"].endswith("Z") and profile["decorations"]["badges"] == []
    assert_error(await anon.get("/users/no-such-user"), 404, "not_found")
    page = await anon.get(f"/users/{author_id}/works", params={"limit": 1000})
    assert page.status_code == 200 and page.json() == {"items": [], "nextCursor": None}
    assert_error(
        await anon.write("POST", f"/users/{author_id}/follow"), 401, "unauthenticated"
    )

    c = Client(app)
    await c.login("plain")
    assert (await c.write("POST", f"/users/{author_id}/follow")).status_code == 204
    assert (await c.write("POST", f"/users/{author_id}/follow")).status_code == 204
    viewer = (await c.get(f"/users/{author_id}")).json()["viewer"]
    assert viewer == {
        "followed": True,
        "blocked": False,
        "friend": False,
        "friendRequest": None,
        "dm": "allowed",
    }
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(AuthorFollow).where(AuthorFollow.fluxer_user_id == me_id)
                )
            )
            .scalars()
            .all()
        )
        assert (
            len(rows) == 1
            and rows[0].active_flag
            and rows[0].fluxer_author_id == author_id
        )

    assert (await c.write("POST", f"/users/{author_id}/block")).status_code == 204
    viewer = (await c.get(f"/users/{author_id}")).json()["viewer"]
    assert viewer["blocked"] is True and viewer["followed"] is False  # 屏蔽后取消关注
    assert (await c.write("DELETE", f"/users/{author_id}/block")).status_code == 204
    assert (await c.write("DELETE", f"/users/{author_id}/follow")).status_code == 204
    async with factory() as session:
        assert (await session.execute(select(SiteUserBlock))).first() is None
    assert_error(
        await c.write("POST", f"/users/{me_id}/follow"), 422, "validation_failed"
    )
    assert_error(await c.write("POST", "/users/nobody/block"), 404, "not_found")

    # 对方设置只收好友私信
    other = Client(app)
    await other.login("author")
    await other.write("PATCH", "/me/prefs", {"dmPrivacy": "friends"})
    assert (await c.get(f"/users/{author_id}")).json()["viewer"]["dm"] == "friends_only"
    await anon.close()
    await c.close()
    await other.close()


@pytest.mark.asyncio
async def test_discord_entry_and_callback_errors(env):
    app, account, _ = env
    c = Client(app)
    res = await c.get("/auth/discord/start", params={"intent": "login"})
    assert res.status_code == 302 and res.headers["location"].startswith(
        "https://discord.com/"
    )
    assert res.headers["X-Content-Mode"] == "general"
    assert_error(
        await c.get("/auth/discord/start", params={"intent": "steal"}),
        422,
        "validation_failed",
    )
    assert_error(
        await c.get("/auth/discord/start", params={"intent": "link"}),
        401,
        "unauthenticated",
    )
    back = await c.get("/auth/discord/callback", params={"error": "access_denied"})
    assert (
        back.status_code == 302
        and back.headers["location"] == "/login?error=access_denied"
    )
    bad = await c.get("/auth/discord/callback", params={"code": "x", "state": "forged"})
    assert bad.headers["location"] == "/login?error=invalid_state"
    await c.close()


@pytest.mark.asyncio
async def test_error_format_scoped_to_api_v1(env):
    app, _, _ = env
    c = Client(app)
    missing = await c.get("/no-such-route")
    assert_error(missing, 404, "not_found")
    boom = await c.get("/__boom")
    err = assert_error(boom, 503, "temporarily_unavailable")
    assert boom.headers["Retry-After"].isdigit() and err["message"]
    # /api/v1 之外保持 FastAPI 默认格式，也不加 X-Content-Mode
    plain = await c.http.get("/plain/missing-validation")
    assert plain.status_code == 422 and "detail" in plain.json()
    assert "X-Content-Mode" not in plain.headers
    await c.close()
