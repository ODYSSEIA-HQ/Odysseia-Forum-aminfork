"""身份头模式（SITE_V1_AUTH_MODE=principal，网关与核心接入约定）。

单元：X-Ody-Principal 校验（跨语言测试向量、改一位、过期、缺 iat）。
接口：FakeFluxer + create_all 的测试库；网关 /internal/users/batch 用 gateway_users.set_fetcher 替换。
"""

import time
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from models.site_account import SiteAccount, SiteUserPrefs
from models.user import User
from models.work import Work
from site_v1 import gateway_users
from site_v1 import principal as principals
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer

SECRET = "test-secret"
VECTOR = (
    "eyJ1c2VySWQiOiJ1X3Rlc3QiLCJtb2RlIjoicjE4Iiwic2hvd1IxOEciOmZhbHNlLCJwZXJtaXNzaW9ucyI6WyJjaGF0"
    "LnNlbmQiXSwibmV3Y29tZXIiOmZhbHNlLCJtdXRlZFVudGlsIjpudWxsLCJibG9ja2VkVXNlcklkcyI6W10sImJsb2Nr"
    "ZWRUYWdJZHMiOltdLCJpYXQiOjE3NjMwOTI4MDAwMDB9.lOEwndIbLOtLLrItLJDZI04ByMsm_X3vPPTcSDUTHFA"
)
VECTOR_NOW = 1763092800000


# ---------------- 单元：校验 ----------------


def test_cross_language_vector():
    p = principals.verify(VECTOR, SECRET, now_ms=VECTOR_NOW)
    assert p is not None
    assert p.user_id == "u_test" and p.mode == "r18" and p.effective_mode == "r18"
    assert p.permissions == frozenset({"chat.send"}) and p.show_r18g is False
    assert p.iat == VECTOR_NOW and p.muted_until is None
    # 前后 60 秒内有效
    assert principals.verify(VECTOR, SECRET, now_ms=VECTOR_NOW + 60_000) is not None
    assert principals.verify(VECTOR, SECRET, now_ms=VECTOR_NOW - 60_000) is not None


def _flip(text: str, index: int) -> str:
    ch = text[index]
    return text[:index] + ("B" if ch != "B" else "C") + text[index + 1 :]


def test_tampered_one_char():
    body, sig = VECTOR.split(".")
    assert (
        principals.verify(_flip(body, 10) + "." + sig, SECRET, now_ms=VECTOR_NOW)
        is None
    )
    assert (
        principals.verify(body + "." + _flip(sig, 5), SECRET, now_ms=VECTOR_NOW) is None
    )
    assert principals.verify(VECTOR, "test-secreT", now_ms=VECTOR_NOW) is None
    # 带填充、多段、空串都不认
    assert principals.verify(VECTOR + "=", SECRET, now_ms=VECTOR_NOW) is None
    assert principals.verify(VECTOR + ".x", SECRET, now_ms=VECTOR_NOW) is None
    assert principals.verify("", SECRET, now_ms=VECTOR_NOW) is None
    assert principals.verify(None, SECRET, now_ms=VECTOR_NOW) is None


def test_expired():
    assert principals.verify(VECTOR, SECRET, now_ms=VECTOR_NOW + 60_001) is None
    assert principals.verify(VECTOR, SECRET, now_ms=VECTOR_NOW - 60_001) is None


def test_missing_or_bad_iat():
    base = {"userId": "u_test", "mode": "general", "permissions": []}
    assert (
        principals.verify(principals.sign(base, SECRET), SECRET, now_ms=VECTOR_NOW)
        is None
    )
    for bad in ("1763092800000", True, None):
        token = principals.sign({**base, "iat": bad}, SECRET)
        assert principals.verify(token, SECRET, now_ms=VECTOR_NOW) is None
    token = principals.sign({**base, "iat": VECTOR_NOW}, SECRET)
    assert principals.verify(token, SECRET, now_ms=VECTOR_NOW) is not None
    # mode 不认识的不收
    token = principals.sign({**base, "mode": "r18g", "iat": VECTOR_NOW}, SECRET)
    assert principals.verify(token, SECRET, now_ms=VECTOR_NOW) is None


def test_sign_matches_vector():
    payload = {
        "userId": "u_test",
        "mode": "r18",
        "showR18G": False,
        "permissions": ["chat.send"],
        "newcomer": False,
        "mutedUntil": None,
        "blockedUserIds": [],
        "blockedTagIds": [],
        "iat": 1763092800000,
    }
    assert principals.sign(payload, SECRET) == VECTOR


def test_principal_mode_requires_secret(monkeypatch):
    monkeypatch.setenv("SITE_V1_AUTH_MODE", "principal")
    monkeypatch.setenv("SITE_V1_PRINCIPAL_SECRET", "")
    with pytest.raises(RuntimeError):
        reload_config()
    monkeypatch.setenv("SITE_V1_AUTH_MODE", "bogus")
    with pytest.raises(RuntimeError):
        reload_config()
    monkeypatch.delenv("SITE_V1_AUTH_MODE")
    monkeypatch.delenv("SITE_V1_PRINCIPAL_SECRET")
    assert reload_config().auth_mode == "session"


# ---------------- 接口 ----------------


@pytest_asyncio.fixture
async def env(monkeypatch, db_session_factory, redis_client):
    monkeypatch.setenv("FLUXER_FAKE", "1")
    monkeypatch.setenv("SITE_V1_AUTH_MODE", "principal")
    monkeypatch.setenv("SITE_V1_PRINCIPAL_SECRET", SECRET)
    reload_config()
    set_session_factory(db_session_factory)
    set_fluxer(FakeFluxer(session_factory=db_session_factory, redis=redis_client))
    await redis_client.flushdb()

    directory: dict[str, dict] = {}
    calls: list[list[str]] = []

    async def fetch(ids):
        calls.append(list(ids))
        return [directory[i] for i in ids if i in directory]

    gateway_users.set_fetcher(fetch)

    from site_v1 import install

    app = FastAPI()
    install(app)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://core.test"
    )

    class Env:
        pass

    e = Env()
    e.http, e.factory, e.directory, e.calls = http, db_session_factory, directory, calls

    def person(username):
        uid = str(uuid.uuid4())
        directory[uid] = {
            "id": uid,
            "username": username,
            "displayName": username.upper(),
            "avatarUrl": None,
        }
        return uid

    def header(user_id, mode="general", **extra):
        payload = {
            "userId": user_id,
            "mode": mode,
            "showR18G": False,
            "permissions": [],
            "newcomer": False,
            "mutedUntil": None,
            "blockedUserIds": [],
            "blockedTagIds": [],
            "iat": int(time.time() * 1000),
            **extra,
        }
        return {"X-Ody-Principal": principals.sign(payload, SECRET)}

    async def req(method, path, user_id=None, mode="general", json=None, **extra):
        return await http.request(
            method, "/api/v1" + path, json=json, headers=header(user_id, mode, **extra)
        )

    e.person, e.header, e.req = person, header, req
    yield e
    await http.aclose()
    gateway_users.set_fetcher(None)
    set_session_factory(None)
    set_fluxer(None)
    for var in ("FLUXER_FAKE", "SITE_V1_AUTH_MODE", "SITE_V1_PRINCIPAL_SECRET"):
        monkeypatch.delenv(var, raising=False)
    reload_config()


async def make_work(env, author_id, rating="general"):
    flags = {"general": (False, False), "r18": (True, False)}[rating]
    async with env.factory() as session:
        work = Work(
            title=f"{rating} 作品",
            author_id=author_id,
            source_platform="fluxer",
            source_thread_id="",
            is_nsfw=flags[0],
            is_abyss=flags[1],
            visibility="public" if rating == "general" else "restricted",
            summary="简介",
            status="published",
            source_visible=True,
        )
        session.add(work)
        await session.flush()
        work_id = work.id
        await session.commit()
        return work_id


@pytest.mark.asyncio
async def test_header_required_and_session_routes_gone(env):
    # 没有头、坏头、过期头：一律 401，不看 Cookie
    res = await env.http.get("/api/v1/me", cookies={"odysseia_sid": "whatever"})
    assert res.status_code == 401 and res.json()["error"]["code"] == "unauthenticated"
    res = await env.http.get("/api/v1/site", headers={"X-Ody-Principal": VECTOR})
    assert res.status_code == 401
    bad = env.header(None)["X-Ody-Principal"][:-2] + "AA"
    assert (
        await env.http.get("/api/v1/site", headers={"X-Ody-Principal": bad})
    ).status_code == 401
    # 未登录也带头：公开接口能用
    res = await env.req("GET", "/site")
    assert res.status_code == 200 and res.headers["x-content-mode"] == "general"
    assert (await env.req("GET", "/me")).status_code == 401
    # 本地登录、注册、会话接口 404
    for method, path in (
        ("POST", "/auth/login"),
        ("POST", "/auth/register"),
        ("POST", "/auth/logout"),
        ("GET", "/session"),
    ):
        res = await env.req(method, path, json={})
        assert res.status_code == 404, (path, res.text)


@pytest.mark.asyncio
async def test_user_row_created_once_from_gateway(env):
    uid = env.person("lin")
    res = await env.req("GET", "/me", uid)
    assert res.status_code == 200, res.text
    me = res.json()
    assert me["id"] == uid and me["username"] == "lin" and me["displayName"] == "LIN"
    assert env.calls == [[uid]]
    # 第二次不再拉资料，也不重复建行
    assert (await env.req("GET", "/me", uid)).status_code == 200
    assert env.calls == [[uid]]
    async with env.factory() as session:
        user = await session.get(User, uid)
        assert user is not None and user.username == "lin"
        assert await session.get(SiteAccount, uid) is not None

    # 网关拉不到资料时先用占位资料建行（ID 来自签名的头，一定存在）
    ghost = str(uuid.uuid4())
    res = await env.req("GET", "/me", ghost)
    assert res.status_code == 200 and res.json()["username"] == ghost


@pytest.mark.asyncio
async def test_mode_from_principal(env):
    author = env.person("author")
    reader = env.person("reader")
    await env.req("GET", "/me", author)
    general_work = await make_work(env, author)
    r18_work = await make_work(env, author, "r18")

    # 本地偏好写成 r18 也没用：有效模式只看 Principal.mode
    await env.req("GET", "/me", reader)
    async with env.factory() as session:
        prefs = await session.get(SiteUserPrefs, reader)
        prefs.content_mode = "r18"
        await session.commit()
    res = await env.req("GET", f"/works/{r18_work}", reader, "general")
    assert res.status_code == 404 and res.headers["x-content-mode"] == "general"
    assert (await env.req("GET", f"/works/{general_work}", reader)).status_code == 200
    res = await env.req("GET", "/works", reader, "general")
    ids = {w["id"] for w in res.json()["items"]}
    assert str(general_work) in ids and str(r18_work) not in ids

    res = await env.req("GET", f"/works/{r18_work}", reader, "r18")
    assert res.status_code == 200 and res.headers["x-content-mode"] == "r18"
    # 未登录的头写 r18 也按 general
    res = await env.req("GET", f"/works/{r18_work}", None, "r18")
    assert res.status_code == 404 and res.headers["x-content-mode"] == "general"
    # r18 交流区同样隔离
    assert (
        await env.req("GET", f"/works/{r18_work}/chat/messages", reader)
    ).status_code == 404
    assert (
        await env.req("GET", f"/works/{r18_work}/chat/messages", reader, "r18")
    ).status_code == 200


@pytest.mark.asyncio
async def test_chat_dm_and_presence_without_csrf(env):
    author = env.person("author")
    alice = env.person("alice")
    bob = env.person("bob")  # 还没来过本核心
    await env.req("GET", "/me", author)
    work = await make_work(env, author)
    base = f"/works/{work}/chat"

    # 写请求不需要 CSRF（网关负责）
    res = await env.req(
        "POST", f"{base}/messages", alice, json={"body": "你好", "nonce": "n1"}
    )
    assert res.status_code == 201, res.text
    assert res.json()["author"]["id"] == alice

    # Principal.mutedUntil：全站禁言以网关为准
    until = "2999-01-01T00:00:00Z"
    res = await env.req(
        "POST", f"{base}/messages", alice, json={"body": "x"}, mutedUntil=until
    )
    assert res.status_code == 403 and res.json()["error"]["code"] == "muted", res.text

    # 私信：对方第一次出现时按网关资料建行
    res = await env.req("POST", "/conversations", alice, json={"userId": bob})
    assert res.status_code == 201, res.text
    conv = res.json()
    assert conv["peer"]["id"] == bob and conv["title"] == "BOB"
    res = await env.req(
        "POST", f"/conversations/{conv['id']}/messages", alice, json={"body": "hi bob"}
    )
    assert res.status_code == 201, res.text
    res = await env.req("GET", f"/conversations/{conv['id']}/messages", bob)
    assert [m["body"] for m in res.json()["items"]] == ["hi bob"]
    # 网关不认识的 ID 不建行
    res = await env.req(
        "POST", "/conversations", alice, json={"userId": str(uuid.uuid4())}
    )
    assert res.status_code == 422
    # 在线状态
    assert (await env.req("PUT", f"{base}/presence", alice)).status_code == 204
    res = await env.req("GET", f"{base}/viewers", bob)
    assert res.json()["count"] == 1 and res.json()["items"][0]["user"]["id"] == alice
    assert (await env.req("GET", f"/users/{alice}/presence", bob)).json() == {
        "online": True
    }
    # Principal.blockedUserIds：本人屏蔽的人不出现在「正在看」里
    res = await env.req("GET", f"{base}/viewers", bob, blockedUserIds=[alice])
    assert res.json()["count"] == 0
    res = await env.req(
        "PUT",
        "/me/presence-settings",
        alice,
        json={"showOnline": True, "showTyping": True, "invisible": True},
    )
    assert res.status_code == 200 and res.json()["invisible"] is True
    assert (await env.req("GET", f"{base}/viewers", bob)).json()["count"] == 0
    assert (await env.req("GET", f"/users/{alice}/presence", bob)).json() == {
        "online": False
    }
    assert (
        await env.req("PUT", "/me/presence-settings", alice, json={"invisible": 1})
    ).status_code == 422
    assert (await env.req("DELETE", f"{base}/presence", alice)).status_code == 204


@pytest.mark.asyncio
async def test_healthz(env):
    res = await env.http.get("/healthz")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "ok" and res.json()["authMode"] == "principal"


@pytest.mark.asyncio
async def test_session_mode_unchanged(env, monkeypatch):
    """默认 session 模式：身份头不起作用，仍按会话 Cookie。"""
    monkeypatch.setenv("SITE_V1_AUTH_MODE", "session")
    reload_config()
    res = await env.http.get("/api/v1/session")
    assert res.status_code == 200 and res.json()["authenticated"] is False
    res = await env.req("GET", "/me", env.person("x"))
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_realtime_recipient_mode_from_last_principal(env):
    """推送给别人时按对方最近一次 Principal 的 mode；没记录按 general。"""
    from site_v1.realtime.events import viewer_ctx

    uid = env.person("rt")
    async with env.factory() as session:
        session.add(User(id=uid, username="rt"))
        await session.commit()
    ctx = await viewer_ctx(uid)
    assert ctx.effective_mode == "general" and ctx.visible_ratings() == {"general"}
    await env.req("GET", "/me", uid, "r18", showR18G=True)
    ctx = await viewer_ctx(uid)
    assert ctx.effective_mode == "r18" and "r18g" in ctx.visible_ratings()
    await env.req("GET", "/me", uid, "general")
    assert (await viewer_ctx(uid)).effective_mode == "general"
