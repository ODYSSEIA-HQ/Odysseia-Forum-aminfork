"""/api/v1 聊天面板接口（3.5 NodeBB 路线参考接口）、私信会话（3.8）、作品内禁言。

FakeFluxer（消息存 site_chat_message）+ create_all 的测试库。
"""

import json
import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from models.attachment import Attachment
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.site_chat import SiteChatMessage, SiteChatMessageMeta
from models.site_moderation import UserModerationRecord
from models.work import Work
from shared.time_utils import utc_now
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.services import chat_notify
from site_v1.services.accounts import ensure_account_rows
from site_v1.services.chat_channels import PublicChannelRef, set_public_channel_source

BASE = "https://site.test"
PASSWORD = "demo1234"


@pytest_asyncio.fixture
async def env(monkeypatch, db_session_factory, redis_client):
    monkeypatch.setenv("FLUXER_FAKE", "1")
    monkeypatch.setenv("SITE_REGISTRATION_MODE", "open")
    monkeypatch.delenv("SITE_COOKIE_SECURE", raising=False)
    reload_config()
    set_session_factory(db_session_factory)
    fake = FakeFluxer(session_factory=db_session_factory, redis=redis_client)
    set_fluxer(fake)
    await redis_client.flushdb()

    from site_v1 import install

    app = FastAPI()
    install(app)

    async def account(
        username, *, access="none", r18=False, permissions=(), dm="everyone"
    ):
        user_id = await fake.ensure_account(
            username,
            PASSWORD,
            email=f"{username}@example.com",
            display_name=username.upper(),
        )
        async with db_session_factory() as session:
            await ensure_account_rows(session, user_id, newcomer=False)
            row = await session.get(SiteRestrictedAccess, user_id)
            row.state = access
            if access == "granted":
                row.age_confirmed_at = utc_now()
                row.terms_version = "2026-10"
            acc = await session.get(SiteAccount, user_id)
            acc.permissions = list(permissions)
            prefs = await session.get(SiteUserPrefs, user_id)
            prefs.content_mode = "r18" if r18 else "general"
            prefs.dm_privacy = dm
            await session.commit()
        return user_id

    class Env:
        pass

    e = Env()
    e.app, e.account, e.factory, e.fake = app, account, db_session_factory, fake
    yield e
    set_session_factory(None)
    set_fluxer(None)
    set_public_channel_source(None, None)
    for var in ("FLUXER_FAKE", "SITE_REGISTRATION_MODE"):
        monkeypatch.delenv(var, raising=False)
    reload_config()


class Client:
    def __init__(self, app):
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        )
        self.csrf = None

    async def session(self):
        res = await self.http.get("/api/v1/session")
        self.csrf = res.json()["csrfToken"]

    async def _write(self, method, path, json=None):
        if self.csrf is None:
            await self.session()
        return await self.http.request(
            method, "/api/v1" + path, json=json, headers={"X-CSRF-Token": self.csrf}
        )

    async def post(self, path, json=None):
        return await self._write("POST", path, json)

    async def patch(self, path, json=None):
        return await self._write("PATCH", path, json)

    async def put(self, path, json=None):
        return await self._write("PUT", path, json)

    async def delete(self, path):
        return await self._write("DELETE", path)

    async def get(self, path, **kw):
        return await self.http.get("/api/v1" + path, **kw)

    async def login(self, username):
        await self.session()
        res = await self.post(
            "/auth/login", {"username": username, "password": PASSWORD}
        )
        assert res.status_code == 200, res.text
        await self.session()
        return self

    async def close(self):
        await self.http.aclose()


async def client(env, username=None):
    c = Client(env.app)
    if username:
        await c.login(username)
    return c


def err(res, status, code):
    assert res.status_code == status, res.text
    body = res.json()["error"]
    assert body["code"] == code, body
    return body


async def make_work(env, author_id, title="作品", rating="general"):
    flags = {"general": (False, False), "r18": (True, False), "r18g": (True, True)}[
        rating
    ]
    async with env.factory() as session:
        work = Work(
            title=title,
            author_id=author_id,
            source_platform="fluxer",
            source_thread_id="",
            is_nsfw=flags[0],
            is_abyss=flags[1],
            visibility="public" if rating == "general" else "restricted",
            summary="简介",
        )
        session.add(work)
        await session.flush()
        work_id = work.id
        await session.commit()
        return work_id


async def make_image(env, owner_id, rating="general", state="ready"):
    async with env.factory() as session:
        att = Attachment(
            owner_id=owner_id,
            purpose="chat",
            kind="image",
            filename="a.png",
            mime="image/png",
            size=10,
            storage_key=f"k/{uuid.uuid4().hex}",
            state=state,
            rating=rating,
        )
        session.add(att)
        await session.flush()
        att_id = att.id
        await session.commit()
        return str(att_id)


@pytest_asyncio.fixture
async def people(env):
    return {
        "author": await env.account("author", access="granted", r18=True),
        "plain": await env.account("plain"),
        "other": await env.account("other"),
        "r18": await env.account("viewer18", access="granted", r18=True),
        "staff": await env.account(
            "staff", permissions=["moderation.content", "channels.manage"]
        ),
    }


# ---------------- 作品交流区 ----------------


@pytest.mark.asyncio
async def test_work_chat_flow(env, people):
    work = await make_work(env, people["author"], "交流区")
    base = f"/works/{work}/chat"
    anon = await client(env)
    plain = await client(env, "plain")
    other = await client(env, "other")
    author = await client(env, "author")

    page = (await anon.get(f"{base}/messages")).json()
    assert page == {"items": [], "hasMoreBefore": False, "hasMoreAfter": False}
    err(await anon.post(f"{base}/messages", {"body": "hi"}), 401, "unauthenticated")
    err(await plain.post(f"{base}/messages", {"body": "  "}), 422, "validation_failed")
    err(
        await plain.post(f"{base}/messages", {"body": "x" * 2001}),
        422,
        "validation_failed",
    )

    res = await plain.post(
        f"{base}/messages",
        {"body": " 你好 ", "nonce": "n1", "attachments": [], "replyTo": None},
    )
    assert res.status_code == 201, res.text
    m1 = res.json()
    assert (
        m1["body"] == "你好"
        and m1["nonce"] == "n1"
        and m1["channelId"] == f"work:{work}"
    )
    assert m1["author"]["id"] == people["plain"] and m1["deletedState"] is None
    assert m1["createdAt"].endswith("Z")
    # 同一个 nonce 重发不重复
    again = await plain.post(f"{base}/messages", {"body": "你好", "nonce": "n1"})
    assert again.status_code == 200 and again.json()["id"] == m1["id"]

    res = await other.post(
        f"{base}/messages", {"body": f"<@{people['plain']}> 回你", "replyTo": m1["id"]}
    )
    assert res.status_code == 201, res.text
    m2 = res.json()
    err(
        await other.post(f"{base}/messages", {"body": "x", "replyTo": "999999"}),
        422,
        "validation_failed",
    )

    # 刷新后还在（不经缓存也一样），nonce 只给本人
    page = (await other.get(f"{base}/messages")).json()
    assert [m["id"] for m in page["items"]] == [m1["id"], m2["id"]]
    assert page["items"][0]["nonce"] is None
    page = (await plain.get(f"{base}/messages", params={"after": m1["id"]})).json()
    assert [m["id"] for m in page["items"]] == [m2["id"]] and page[
        "hasMoreAfter"
    ] is False
    page = (await plain.get(f"{base}/messages", params={"before": m2["id"]})).json()
    assert [m["id"] for m in page["items"]] == [m1["id"]] and page[
        "hasMoreBefore"
    ] is False
    page = (
        await plain.get(f"{base}/messages", params={"around": m1["id"], "limit": 1})
    ).json()
    assert [m["id"] for m in page["items"]] == [m1["id"]] and page[
        "hasMoreAfter"
    ] is True
    err(
        await plain.get(f"{base}/messages", params={"before": "123456789"}),
        404,
        "not_found",
    )
    err(await plain.get(f"{base}/messages", params={"after": "abc"}), 404, "not_found")

    # 未读：plain 发过言（已读到 m1），m2 @了他
    state = (await plain.get(f"{base}/state")).json()
    assert (
        state["lastReadMessageId"] == m1["id"]
        and state["unread"] == 1
        and state["mentions"] == 1
    )
    assert (
        state["features"] == {"attach": True, "poll": True, "fun": True}
        and state["mutedUntil"] is None
    )
    summary = (await plain.get("/chat/unread-summary")).json()
    assert {"target": {"kind": "work", "id": str(work)}, "count": 1, "mentions": 1} == {
        k: v for k, v in summary[0].items() if k != "lastMessageAt"
    }
    state = (await plain.post(f"{base}/read", {"messageId": m2["id"]})).json()
    assert state["unread"] == 0 and state["lastReadMessageId"] == m2["id"]
    # 已读不倒退
    state = (await plain.post(f"{base}/read", {"messageId": m1["id"]})).json()
    assert state["lastReadMessageId"] == m2["id"]
    err(await plain.post(f"{base}/read", {}), 422, "validation_failed")

    # 编辑：只能编辑自己的
    err(
        await other.patch(f"{base}/messages/{m1['id']}", {"body": "改"}),
        403,
        "forbidden",
    )
    res = await plain.patch(f"{base}/messages/{m1['id']}", {"body": "改过了"})
    assert (
        res.status_code == 200
        and res.json()["body"] == "改过了"
        and res.json()["editedAt"]
    )

    # 表情
    res = await other.put(f"{base}/messages/{m1['id']}/reactions/👍")
    assert res.json()["reactions"] == [{"emoji": "👍", "count": 1, "me": True}]
    res = await plain.put(f"{base}/messages/{m1['id']}/reactions/👍")
    assert res.json()["reactions"] == [{"emoji": "👍", "count": 2, "me": True}]
    page = (await anon.get(f"{base}/messages")).json()
    assert page["items"][0]["reactions"] == [{"emoji": "👍", "count": 2, "me": False}]
    res = await plain.delete(f"{base}/messages/{m1['id']}/reactions/👍")
    assert res.json()["reactions"] == [{"emoji": "👍", "count": 1, "me": False}]

    # 删除：别人不能删，作者（卡贴管理员）能删，留占位
    err(await other.delete(f"{base}/messages/{m1['id']}"), 403, "forbidden")
    res = await author.delete(f"{base}/messages/{m1['id']}")
    assert res.status_code == 200
    deleted = res.json()
    assert (
        deleted["deletedState"] == "by_manager"
        and deleted["body"] == ""
        and deleted["reactions"] == []
    )
    res = await other.delete(f"{base}/messages/{m2['id']}")
    assert res.json()["deletedState"] == "by_author"
    page = (await anon.get(f"{base}/messages")).json()
    assert [m["deletedState"] for m in page["items"]] == ["by_manager", "by_author"]
    err(
        await plain.patch(f"{base}/messages/{m1['id']}", {"body": "x"}), 409, "conflict"
    )
    err(await plain.put(f"{base}/messages/{m1['id']}/reactions/👍"), 409, "conflict")
    async with env.factory() as session:
        meta = await session.get(SiteChatMessageMeta, (f"work-{work}", m1["id"]))
        assert meta.deleted_by == people["author"]

    assert (await plain.post(f"{base}/typing")).status_code == 204
    for c in (anon, plain, other, author):
        await c.close()


@pytest.mark.asyncio
async def test_work_chat_r18_isolation_and_mutes(env, people):
    general = await make_work(env, people["author"], "公开")
    restricted = await make_work(env, people["author"], "受限", "r18")
    plain = await client(env, "plain")
    r18 = await client(env, "viewer18")
    author = await client(env, "author")

    # 全年龄模式看不到受限作品交流区
    for path in ("messages", "state"):
        err(await plain.get(f"/works/{restricted}/chat/{path}"), 404, "not_found")
    err(
        await plain.post(f"/works/{restricted}/chat/messages", {"body": "x"}),
        404,
        "not_found",
    )
    assert (
        await r18.post(f"/works/{restricted}/chat/messages", {"body": "x"})
    ).status_code == 201

    # public 作品不能发 R18 图；受限作品可以，全年龄看不到（404）
    img18 = await make_image(env, people["r18"], "r18")
    e = err(
        await r18.post(
            f"/works/{general}/chat/messages", {"body": "", "attachments": [img18]}
        ),
        422,
        "rating_conflict",
    )
    assert e["details"]["rating"] == "r18"
    res = await r18.post(
        f"/works/{restricted}/chat/messages", {"body": "", "attachments": [img18]}
    )
    assert res.status_code == 201, res.text
    assert (
        res.json()["attachments"][0]["id"] == img18
        and res.json()["attachments"][0]["placeholder"] is False
    )
    pending = await make_image(env, people["r18"], "general", state="processing")
    err(
        await r18.post(f"/works/{general}/chat/messages", {"attachments": [pending]}),
        422,
        "validation_failed",
    )
    foreign = await make_image(env, people["plain"])
    err(
        await r18.post(f"/works/{general}/chat/messages", {"attachments": [foreign]}),
        422,
        "validation_failed",
    )

    # 作品内禁言：作者禁言 plain → 发送 403 muted；state 给截止时间
    err(
        await plain.post(
            f"/works/{general}/mutes", {"userId": people["other"], "until": None}
        ),
        403,
        "forbidden",
    )
    err(
        await author.post(
            f"/works/{general}/mutes", {"userId": people["author"], "until": None}
        ),
        422,
        "validation_failed",
    )
    err(
        await author.post(
            f"/works/{general}/mutes",
            {"userId": people["plain"], "until": "2000-01-01T00:00:00Z"},
        ),
        422,
        "validation_failed",
    )
    res = await author.post(
        f"/works/{general}/mutes",
        {"userId": people["plain"], "until": None, "reason": "刷屏"},
    )
    assert res.status_code == 201, res.text
    assert res.json() == {
        "workId": str(general),
        "userId": people["plain"],
        "until": None,
        "reason": "刷屏",
    }
    e = err(
        await plain.post(f"/works/{general}/chat/messages", {"body": "hi"}),
        403,
        "muted",
    )
    assert e["details"]["until"] is None
    assert (await plain.get(f"/works/{general}/chat/state")).json()[
        "mutedUntil"
    ] == "9999-12-31T00:00:00.000Z"
    assert (await plain.get(f"/chat/targets/work/{general}")).json()["canPost"] is False
    until = (utc_now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = await author.post(
        f"/works/{general}/mutes", {"userId": people["plain"], "until": until}
    )
    assert res.json()["until"].startswith(until[:19])
    assert (
        (await plain.get(f"/works/{general}/chat/state"))
        .json()["mutedUntil"]
        .startswith(until[:16])
    )
    assert (
        await author.delete(f"/works/{general}/mutes/{people['plain']}")
    ).status_code == 204
    assert (
        await plain.post(f"/works/{general}/chat/messages", {"body": "hi"})
    ).status_code == 201

    # 全站禁言
    async with env.factory() as session:
        session.add(
            UserModerationRecord(
                user_id=people["other"],
                kind="mute",
                until=utc_now() + timedelta(hours=1),
                by_user_id=people["staff"],
            )
        )
        await session.commit()
    other = await client(env, "other")
    err(
        await other.post(f"/works/{general}/chat/messages", {"body": "hi"}),
        403,
        "muted",
    )
    for c in (plain, r18, author, other):
        await c.close()


@pytest.mark.asyncio
async def test_mentions_notify_and_cache(env, people, monkeypatch):
    work = await make_work(env, people["author"], "提醒")
    delivered = []

    async def capture(session, recipient, notification):
        delivered.append((recipient, notification))

    monkeypatch.setattr(chat_notify, "deliver_notification", capture)
    plain = await client(env, "plain")
    res = await plain.post(
        f"/works/{work}/chat/messages",
        {"body": f"<@{people['other']}> <@{people['plain']}> <@nobody> 看这里"},
    )
    assert res.status_code == 201
    assert [r for r, _ in delivered] == [people["other"]]
    n = delivered[0][1]
    assert n["category"] == "mention" and n["target"] == {
        "type": "work",
        "id": str(work),
        "title": "提醒",
    }
    assert n["context"] == {"workId": str(work), "messageId": res.json()["id"]}
    assert (
        n["excerpt"].startswith("@OTHER @PLAIN") and n["actor"]["id"] == people["plain"]
    )

    # 缓存：最新 50 条在 Redis；直接写进假 Fluxer 的消息在缓存过期 / 写操作前看不到
    await plain.get(f"/works/{work}/chat/messages")
    async with env.factory() as session:
        session.add(
            SiteChatMessage(
                channel_id=f"work-{work}", author_id=people["other"], content="绕过本站"
            )
        )
        await session.commit()
    page = (await plain.get(f"/works/{work}/chat/messages")).json()
    assert len(page["items"]) == 1
    await plain.post(f"/works/{work}/chat/messages", {"body": "再来"})
    page = (await plain.get(f"/works/{work}/chat/messages")).json()
    assert [m["body"] for m in page["items"]][-2:] == ["绕过本站", "再来"]

    # 超过 50 条：hasMoreBefore，往前翻
    async with env.factory() as session:
        session.add_all(
            SiteChatMessage(
                channel_id=f"work-{work}", author_id=people["other"], content=f"m{i}"
            )
            for i in range(60)
        )
        await session.commit()
    from site_v1.services.chat_messages import invalidate

    await invalidate(f"work-{work}")
    page = (await plain.get(f"/works/{work}/chat/messages")).json()
    assert (
        len(page["items"]) == 50
        and page["hasMoreBefore"] is True
        and page["items"][-1]["body"] == "m59"
    )
    older = (
        await plain.get(
            f"/works/{work}/chat/messages", params={"before": page["items"][0]["id"]}
        )
    ).json()
    assert (
        len(older["items"]) == 13
        and older["hasMoreBefore"] is False
        and older["hasMoreAfter"] is True
    )
    await plain.close()


# ---------------- 私信 ----------------


@pytest.mark.asyncio
async def test_conversations(env, people):
    friends_only = await env.account("shy", dm="friends")
    closed = await env.account("closed", dm="nobody")
    plain = await client(env, "plain")
    other = await client(env, "other")
    anon = await client(env)

    err(await anon.get("/conversations"), 401, "unauthenticated")
    assert (await plain.get("/conversations")).json() == {
        "items": [],
        "nextCursor": None,
    }
    err(await plain.post("/conversations", {}), 422, "validation_failed")
    err(
        await plain.post("/conversations", {"userId": "nobody-here"}),
        422,
        "validation_failed",
    )
    err(
        await plain.post("/conversations", {"userId": people["plain"]}),
        422,
        "validation_failed",
    )
    err(await plain.post("/conversations", {"userId": friends_only}), 403, "forbidden")
    err(await plain.post("/conversations", {"userId": closed}), 403, "forbidden")

    res = await plain.post("/conversations", {"userId": people["other"]})
    assert res.status_code == 201, res.text
    conv = res.json()
    assert (
        conv["type"] == "dm"
        and conv["peer"]["id"] == people["other"]
        and conv["title"] == "OTHER"
    )
    assert conv["lastMessage"] is None and conv["unread"] == 0
    res = await other.post(
        "/conversations", {"userId": "plain"}
    )  # 用户名也行，已有返回 200
    assert res.status_code == 200 and res.json()["id"] == conv["id"]

    base = f"/conversations/{conv['id']}"
    res = await plain.post(f"{base}/messages", {"body": f"嗨 <@{people['other']}>"})
    assert res.status_code == 201, res.text
    # 私信里的 R18 图：发送方可以发，全年龄的对方只看到占位
    img18 = await make_image(env, people["plain"], "r18")
    res = await plain.post(f"{base}/messages", {"body": "", "attachments": [img18]})
    assert res.status_code == 201
    page = (await other.get(f"{base}/messages")).json()
    att = page["items"][1]["attachments"][0]
    assert att["placeholder"] is True and att["url"] is None

    convs = (await other.get("/conversations")).json()["items"]
    assert (
        len(convs) == 1
        and convs[0]["unread"] == 2
        and convs[0]["lastMessage"]["excerpt"] == "[图片]"
    )
    state = (await other.get(f"{base}/state")).json()
    assert (
        state["unread"] == 2
        and state["mentions"] == 1
        and state["lastReadMessageId"] is None
    )
    summary = (await other.get("/chat/unread-summary")).json()
    assert any(
        i["target"] == {"kind": "dm", "id": conv["id"]} and i["count"] == 2
        for i in summary
    )
    info = (await other.get(f"/chat/targets/dm/{conv['id']}")).json()
    assert info["title"] == "PLAIN" and info["memberCount"] == 2

    # 非成员 404；对方改成只收好友后，每条消息都拦
    r18 = await client(env, "viewer18")
    err(await r18.get(f"{base}/messages"), 404, "not_found")
    err(await r18.post(f"{base}/messages", {"body": "x"}), 404, "not_found")
    async with env.factory() as session:
        prefs = await session.get(SiteUserPrefs, people["other"])
        prefs.dm_privacy = "nobody"
        await session.commit()
    err(await plain.post(f"{base}/messages", {"body": "还在吗"}), 403, "forbidden")
    for c in (plain, other, anon, r18):
        await c.close()


# ---------------- 公共聊天区 ----------------


@pytest.mark.asyncio
async def test_public_channel_chat(env, people):
    channels = {
        "lobby": PublicChannelRef(
            id="lobby", name="大厅", rating="general", fluxer_channel_id=None
        ),
        "night": PublicChannelRef(
            id="night", name="夜聊", rating="r18", fluxer_channel_id="9001"
        ),
    }

    async def get(session, cid):
        return channels.get(cid)

    async def list_(session):
        return list(channels.values())

    set_public_channel_source(get, list_)
    plain = await client(env, "plain")
    other = await client(env, "other")
    r18 = await client(env, "viewer18")
    staff = await client(env, "staff")

    err(await plain.get("/channels/night/chat/messages"), 404, "not_found")
    err(await plain.get("/chat/targets/channel/night"), 404, "not_found")
    assert (await r18.get("/channels/night/chat/messages")).status_code == 200
    info = (await plain.get("/chat/targets/channel/lobby")).json()
    assert (
        info["canOpen"] is True and info["title"] == "大厅" and info["canPost"] is True
    )

    res = await plain.post("/channels/lobby/chat/messages", {"body": "大家好"})
    assert res.status_code == 201 and res.json()["channelId"] == "channel:lobby"
    img18 = await make_image(env, people["r18"], "r18")
    err(
        await r18.post("/channels/lobby/chat/messages", {"attachments": [img18]}),
        422,
        "rating_conflict",
    )
    assert (
        await r18.post("/channels/night/chat/messages", {"attachments": [img18]})
    ).status_code == 201

    err(
        await other.delete(f"/channels/lobby/chat/messages/{res.json()['id']}"),
        403,
        "forbidden",
    )
    res = await staff.delete(f"/channels/lobby/chat/messages/{res.json()['id']}")
    assert res.json()["deletedState"] == "by_staff"

    summary = (await plain.get("/chat/unread-summary")).json()
    targets = [i["target"] for i in summary]
    assert {"kind": "channel", "id": "lobby"} in targets and {
        "kind": "channel",
        "id": "night",
    } not in targets
    assert {"kind": "channel", "id": "night"} in [
        i["target"] for i in (await r18.get("/chat/unread-summary")).json()
    ]
    async with env.factory() as session:
        rows = (
            (await session.execute(select(SiteChatMessage.channel_id))).scalars().all()
        )
        assert set(rows) == {"channel-lobby", "9001"}
    for c in (plain, other, r18, staff):
        await c.close()


# ---------------- 真实实现：Fluxer 原生 HTTP ----------------


@pytest.mark.asyncio
async def test_native_http_mapping(monkeypatch):
    from site_v1.fluxer import native_http
    from site_v1.fluxer.real import RealFluxer

    seen = []

    def handler(request: httpx.Request):
        seen.append(
            (
                request.method,
                request.url.path,
                dict(request.url.params),
                request.headers.get("authorization"),
                request.content,
            )
        )
        if request.url.path.endswith("/messages") and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "200",
                        "channel_id": "77",
                        "content": "新",
                        "timestamp": "2026-11-01T10:00:01+00:00",
                        "edited_timestamp": None,
                        "author": {"id": "5", "username": "u5"},
                        "attachments": [],
                        "reactions": [
                            {"emoji": {"name": "👍"}, "count": 2, "me": True}
                        ],
                        "message_reference": {"message_id": "100"},
                        "referenced_message": {"author": {"id": "6"}},
                    },
                    {
                        "id": "100",
                        "channel_id": "77",
                        "content": "旧",
                        "timestamp": "2026-11-01T10:00:00Z",
                        "author": {"id": "6", "username": "u6"},
                        "attachments": [
                            {
                                "id": "9",
                                "url": "https://cdn/x.png",
                                "filename": "x.png",
                                "content_type": "image/png",
                                "size": 3,
                            }
                        ],
                    },
                ],
            )
        if request.url.path.endswith("/messages/404"):
            return httpx.Response(
                404, json={"code": 10008, "message": "Unknown Message"}
            )
        if request.url.path == "/v1/users/@me/channels" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "88",
                        "type": 1,
                        "recipients": [{"id": "6"}],
                        "last_message_id": "300",
                    },
                    {"id": "99", "type": 3},
                ],
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            403, json={"code": 50013, "message": "Missing Permissions"}
        )

    monkeypatch.setenv("FLUXER_API_URL", "https://fluxer.test/v1/")
    monkeypatch.setenv("FLUXER_BOT_TOKEN", "bot-secret")
    native_http.set_http_client(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        real = RealFluxer(auth_service=object(), session_factory=None)
        items = await real.chat_messages("flx_tok", "5", "77", after="50", limit=2)
        assert [m.id for m in items] == ["100", "200"]
        assert items[1].reply_to == "100" and items[1].reply_to_author_id == "6"
        assert items[1].reactions == [
            {"emoji": "👍", "count": 2, "me": True, "user_ids": None}
        ]
        assert items[0].attachments[0]["content_type"] == "image/png"
        assert seen[0][:4] == (
            "GET",
            "/v1/channels/77/messages",
            {"limit": "2", "after": "50"},
            "flx_tok",
        )
        assert await real.chat_message("flx_tok", "5", "77", "404") is None
        assert await real.chat_dm_channels("flx_tok", "5") == [
            {
                "channel_id": "88",
                "peer_user_id": "6",
                "created_at": None,
                "last_message_id": "300",
            }
        ]
        await real.chat_delete(
            "flx_tok", "5", "77", "100", deleted_state="by_manager", as_moderator=True
        )
        assert seen[-1][3] == "Bot bot-secret"
        from site_v1.fluxer.port import FluxerForbidden

        with pytest.raises(FluxerForbidden):
            await real.chat_send(
                "flx_tok", "5", "77", content="x", nonce="n", reply_to=None
            )
        assert json.loads(seen[-1][4]) == {
            "content": "x",
            "nonce": "n",
            "enforce_nonce": True,
        }
        monkeypatch.delenv("FLUXER_API_URL")
        from site_v1.fluxer.port import FluxerUnavailable

        with pytest.raises(FluxerUnavailable):
            await real.chat_typing("flx_tok", "5", "77")
    finally:
        native_http.set_http_client(None)
