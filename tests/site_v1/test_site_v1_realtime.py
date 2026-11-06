"""/api/v1 实时推送（Centrifugo）：令牌签发和签名、订阅令牌权限、发布到 HTTP server API。

假 Centrifugo 是测试里起的一个最小 FastAPI（接 POST /api/{method}，记下请求），
经 publisher.set_transport(httpx.ASGITransport) 接上。推送的数据形状用 odysseia-hq/api 的
realtime-events.schema.json（外层事件，对象引用 openapi.yaml）校验；面板事件
（message.created / message.updated / typing）协议 schema 里没有，按前端
src/shared/protocol/chatMessage.ts 的 ChatChannelEvent 写成 schema，target 用
panel-messages.schema.json 的 ChatTarget、author / attachments 用 openapi 的 User / Attachment。
API_REPO（默认 /home/user/api）不存在时跳过 schema 校验。
"""

# ruff: noqa: F811 — env / people 等 fixture 从聊天测试导入，测试函数参数同名

import json
import os
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy import update

from models.site_me import SiteAnnouncement
from models.work import Work
from shared.time_utils import utc_now
from site_v1.realtime import jwt, publisher
from site_v1.realtime.config import reload_realtime_config
from site_v1.services.chat_channels import PublicChannelRef, set_public_channel_source

from test_site_v1_chat_messages_api import (  # noqa: F401 — fixtures
    client,
    env,
    err,
    make_image,
    make_work,
    people,
)

SECRET = "test-hmac-secret"
API_KEY = "test-api-key"
PUBLIC_URL = "ws://rt.test/connection/websocket"
SPEC_DIR = Path(os.environ.get("API_REPO", "/home/user/api")) / "openapi"
BASE = "https://odysseia.invalid/api/"

CENTRIFUGO_VARS = (
    "CENTRIFUGO_API_URL",
    "CENTRIFUGO_API_KEY",
    "CENTRIFUGO_TOKEN_SECRET",
    "CENTRIFUGO_PUBLIC_URL",
)


# ---------------- schema ----------------


class Schemas:
    def __init__(self):
        import yaml
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        spec = yaml.safe_load((SPEC_DIR / "openapi.yaml").read_text("utf-8"))
        realtime = json.loads(
            (SPEC_DIR / "realtime-events.schema.json").read_text("utf-8")
        )
        panel = json.loads((SPEC_DIR / "panel-messages.schema.json").read_text("utf-8"))
        registry = Registry().with_resources(
            [
                (BASE + "openapi.yaml", Resource.from_contents(spec, DRAFT202012)),
                (realtime["$id"], Resource.from_contents(realtime, DRAFT202012)),
                (panel["$id"], Resource.from_contents(panel, DRAFT202012)),
            ]
        )
        component = BASE + "openapi.yaml#/components/schemas/"
        message = {
            "type": "object",
            "required": [
                "id",
                "channelId",
                "author",
                "body",
                "attachments",
                "replyTo",
                "reactions",
                "editedAt",
                "deletedState",
                "createdAt",
            ],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "channelId": {"type": "string", "minLength": 1},
                "author": {"$ref": component + "User"},
                "body": {"type": "string"},
                "attachments": {
                    "type": "array",
                    "items": {"$ref": component + "Attachment"},
                },
                "replyTo": {"type": ["string", "null"]},
                "reactions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["emoji", "count", "me"],
                        "properties": {
                            "emoji": {"type": "string"},
                            "count": {"type": "integer", "minimum": 1},
                            "me": {"type": "boolean"},
                        },
                    },
                },
                "editedAt": {"type": ["string", "null"]},
                "deletedState": {"enum": [None, "by_author", "by_manager", "by_staff"]},
                "createdAt": {"type": "string"},
                "nonce": {"type": ["string", "null"]},
            },
        }
        target = {
            "anyOf": [
                {"$ref": panel["$id"] + "#/$defs/ChatTarget"},
                {
                    "type": "object",
                    "required": ["kind", "id"],
                    "properties": {
                        "kind": {"const": "channel"},
                        "id": {"type": "string", "minLength": 1},
                    },
                },
            ]
        }
        panel_event = {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["type", "target", "message"],
                    "properties": {
                        "type": {"enum": ["message.created", "message.updated"]},
                        "target": target,
                        "message": message,
                    },
                },
                {
                    "type": "object",
                    "required": ["type", "target", "userId"],
                    "properties": {
                        "type": {"const": "typing"},
                        "target": target,
                        "userId": {"type": "string", "minLength": 1},
                    },
                },
            ]
        }
        self.outer = Draft202012Validator(realtime, registry=registry)
        self.panel = Draft202012Validator(panel_event, registry=registry)
        self.component = lambda name: Draft202012Validator(
            {"$ref": component + name}, registry=registry
        )


_schemas: Schemas | None = None


def schemas() -> Schemas | None:
    global _schemas
    if not (SPEC_DIR / "openapi.yaml").exists():
        return None
    if _schemas is None:
        _schemas = Schemas()
    return _schemas


def check_outer(data: dict) -> None:
    s = schemas()
    if s is not None:
        s.outer.validate(data)
    assert set(data) == {"type", "id", "ts", "data"}


def check_panel(data: dict) -> None:
    s = schemas()
    if s is not None:
        s.panel.validate(data)


# ---------------- 假 Centrifugo ----------------


class FakeCentrifugo:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        app = FastAPI()

        @app.post("/api/{method}")
        async def api(method: str, request: Request):
            assert request.headers.get("x-api-key") == API_KEY
            if self.fail:
                return _error()
            self.calls.append((method, await request.json()))
            return {"result": {}}

        self.app = app

    def publishes(
        self, channel: str | None = None, type_: str | None = None
    ) -> list[dict]:
        return [
            params["data"]
            for method, params in self.calls
            if method == "publish"
            and (channel is None or params["channel"] == channel)
            and (type_ is None or params["data"].get("type") == type_)
        ]

    def channels(self) -> list[str]:
        return [p["channel"] for m, p in self.calls if m == "publish"]

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


def _error():
    from fastapi.responses import JSONResponse

    return JSONResponse(
        {"error": {"code": 100, "message": "internal"}}, status_code=500
    )


@pytest_asyncio.fixture
async def rt(env, monkeypatch):
    monkeypatch.setenv("CENTRIFUGO_API_URL", "http://centrifugo.test")
    monkeypatch.setenv("CENTRIFUGO_API_KEY", API_KEY)
    monkeypatch.setenv("CENTRIFUGO_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("CENTRIFUGO_PUBLIC_URL", PUBLIC_URL)
    reload_realtime_config()
    fake = FakeCentrifugo()
    publisher.set_transport(httpx.ASGITransport(app=fake.app))
    yield fake
    await publisher.drain()
    publisher.set_transport(None)
    for var in CENTRIFUGO_VARS:
        monkeypatch.delenv(var, raising=False)
    reload_realtime_config()


@pytest.fixture
def no_rt(monkeypatch):
    for var in CENTRIFUGO_VARS:
        monkeypatch.delenv(var, raising=False)
    reload_realtime_config()
    yield
    reload_realtime_config()


async def settle(fake: FakeCentrifugo | None = None):
    await publisher.drain()


# ---------------- 令牌 ----------------


def test_jwt_sign_and_verify():
    now = int(time.time())
    token = jwt.encode({"sub": "u1", "exp": now + 60, "channels": ["site"]}, SECRET)
    header, payload, _ = token.split(".")
    assert json.loads(jwt._unb64(header)) == {"alg": "HS256", "typ": "JWT"}
    assert jwt.decode(token, SECRET)["channels"] == ["site"]
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(token, "other-secret")
    forged = jwt._b64(json.dumps({"sub": "admin", "exp": now + 60}).encode())
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(f"{header}.{forged}.{token.split('.')[2]}", SECRET)
    with pytest.raises(jwt.InvalidToken):
        jwt.decode(jwt.encode({"sub": "u1", "exp": now - 1}, SECRET), SECRET)
    with pytest.raises(jwt.InvalidToken):
        jwt.decode("not-a-token", SECRET)
    # 和标准 HS256 实现一致：用 hmac 手算一遍
    import base64
    import hashlib
    import hmac

    sig = hmac.new(
        SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    assert base64.urlsafe_b64encode(sig).rstrip(b"=").decode() == token.split(".")[2]


@pytest.mark.asyncio
async def test_disabled_returns_503_and_publish_is_noop(env, people, no_rt):
    fake = FakeCentrifugo()
    publisher.set_transport(httpx.ASGITransport(app=fake.app))
    try:
        plain = await client(env, "plain")
        err(await plain.get("/realtime/token"), 503, "temporarily_unavailable")
        err(
            await plain.get("/realtime/subscribe-token", params={"channel": "site"}),
            503,
            "temporarily_unavailable",
        )
        work_id = await make_work(env, people["author"])
        res = await plain.post(f"/works/{work_id}/chat/messages", {"body": "hi"})
        assert res.status_code == 201
        await settle()
        assert fake.calls == []
        await plain.close()
    finally:
        publisher.set_transport(None)


@pytest.mark.asyncio
async def test_connection_token(env, people, rt):
    anon = await client(env)
    res = await anon.get("/realtime/token")
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    body = res.json()
    assert set(body) == {"url", "token"} and body["url"] == PUBLIC_URL
    claims = jwt.decode(body["token"], SECRET)
    assert claims["sub"] == "" and claims["channels"] == ["site"]
    assert claims["exp"] > time.time() + 60

    plain = await client(env, "plain")
    claims = jwt.decode((await plain.get("/realtime/token")).json()["token"], SECRET)
    assert claims["sub"] == people["plain"]
    assert claims["channels"] == [f"personal:{people['plain']}", "site"]

    r18 = await client(env, "viewer18")
    claims = jwt.decode((await r18.get("/realtime/token")).json()["token"], SECRET)
    assert claims["channels"] == [f"personal:{people['r18']}", "site", "site_r18"]

    # 有资格但没切到 R18 模式：没有 site_r18
    await env.account("granted_general", access="granted", r18=False)
    gg = await client(env, "granted_general")
    claims = jwt.decode((await gg.get("/realtime/token")).json()["token"], SECRET)
    assert "site_r18" not in claims["channels"]
    for c in (anon, plain, r18, gg):
        await c.close()


@pytest.mark.asyncio
async def test_subscribe_token_permissions(env, people, rt):
    channels_src = {
        "lobby": PublicChannelRef(
            id="lobby", name="大厅", rating="general", fluxer_channel_id=None
        ),
        "night": PublicChannelRef(
            id="night", name="夜聊", rating="r18", fluxer_channel_id="9001"
        ),
    }

    async def get(session, cid):
        return channels_src.get(cid)

    async def list_(session):
        return list(channels_src.values())

    set_public_channel_source(get, list_)
    general = await make_work(env, people["author"], "全年龄")
    restricted = await make_work(env, people["author"], "受限", "r18")
    r18g = await make_work(env, people["author"], "R18G", "r18g")
    anon = await client(env)
    plain = await client(env, "plain")
    r18 = await client(env, "viewer18")

    async def token(c, channel):
        return await c.get("/realtime/subscribe-token", params={"channel": channel})

    async def ok(c, channel, sub):
        res = await token(c, channel)
        assert res.status_code == 200, (channel, res.text)
        claims = jwt.decode(res.json()["token"], SECRET)
        assert claims["channel"] == channel and claims["sub"] == sub
        assert claims["exp"] > time.time()

    # 全年龄作品：谁都能订作品页、交流区
    await ok(anon, f"work:{general}", "")
    await ok(plain, f"chat:work.{general}", people["plain"])
    # 受限作品：全年龄模式 404（看不到 = 不存在），R18 模式可以
    for channel in (f"work:{restricted}", f"chat:work.{restricted}"):
        err(await token(plain, channel), 404, "not_found")
        err(await token(anon, channel), 404, "not_found")
        await ok(r18, channel, people["r18"])
    # R18G 要开 showR18G
    err(await token(r18, f"work:{r18g}"), 404, "not_found")
    # 不存在的作品
    err(await token(r18, "work:999999999"), 404, "not_found")
    # 公共聊天区按频道分级
    await ok(plain, "chat:channel.lobby", people["plain"])
    err(await token(plain, "chat:channel.night"), 404, "not_found")
    await ok(r18, "chat:channel.night", people["r18"])
    # 本人频道：只能是自己的
    await ok(plain, f"personal:{people['plain']}", people["plain"])
    err(await token(plain, f"personal:{people['other']}"), 403, "forbidden")
    err(await token(anon, f"personal:{people['other']}"), 403, "forbidden")
    # site_r18：只给 R18 模式
    err(await token(plain, "site_r18"), 403, "forbidden")
    await ok(r18, "site_r18", people["r18"])
    await ok(anon, "site", "")
    # 私信不单独订阅（走本人频道）；其它名字 404
    for bad in ("chat:dm.123", "", "nonsense", "work:abc", "chat:bogus.1", "x" * 300):
        err(await token(plain, bad), 404, "not_found")
    for c in (anon, plain, r18):
        await c.close()


# ---------------- 发布：聊天 ----------------


@pytest.mark.asyncio
async def test_work_chat_publishes_created_updated_deleted(env, people, rt):
    work_id = await make_work(env, people["author"])
    channel = f"chat:work.{work_id}"
    plain = await client(env, "plain")
    other = await client(env, "other")
    base = f"/works/{work_id}/chat/messages"

    t0 = utc_now()
    sent = (await plain.post(base, {"body": "第一条", "nonce": "n-1"})).json()
    await settle()
    created = rt.publishes(channel, "message.created")
    assert len(created) == 1
    event = created[0]
    check_panel(event)
    assert event["target"] == {"kind": "work", "id": str(work_id)}
    assert event["message"]["id"] == sent["id"] and event["message"]["body"] == "第一条"
    assert event["message"]["nonce"] == "n-1"

    res = await plain.patch(f"{base}/{sent['id']}", {"body": "改过了"})
    assert res.status_code == 200
    await settle()
    updated = rt.publishes(channel, "message.updated")
    assert len(updated) == 1 and updated[0]["message"]["body"] == "改过了"
    assert updated[0]["message"]["editedAt"] is not None
    check_panel(updated[0])

    res = await plain.put(f"{base}/{sent['id']}/reactions/👍")
    assert res.status_code == 200 and res.json()["reactions"][0]["me"] is True
    await settle()
    reacted = rt.publishes(channel, "message.updated")[-1]
    check_panel(reacted)
    # 频道广播里 me 一律 false（每个人不同）
    assert reacted["message"]["reactions"] == [{"emoji": "👍", "count": 1, "me": False}]

    res = await plain.delete(f"{base}/{sent['id']}")
    assert res.status_code == 200
    await settle()
    deleted = rt.publishes(channel, "message.updated")[-1]
    check_panel(deleted)
    # 删除 = message.updated：整条消息，deletedState 有值、body 为空串
    assert deleted["message"]["id"] == sent["id"]
    assert deleted["message"]["deletedState"] == "by_author"
    assert deleted["message"]["body"] == "" and deleted["message"]["attachments"] == []

    res = await other.post(f"/works/{work_id}/chat/typing")
    assert res.status_code == 204
    await settle()
    typing = rt.publishes(channel, "typing")
    assert typing == [
        {
            "type": "typing",
            "target": {"kind": "work", "id": str(work_id)},
            "userId": people["other"],
        }
    ]
    check_panel(typing[0])

    # 轮询：updatedAfter 拿到编辑、删除过的消息（整条），并给 serverTime
    second = (await other.post(base, {"body": "第二条"})).json()
    page = (
        await other.get(
            base,
            params={"after": sent["id"], "updatedAfter": t0.isoformat() + "Z"},
        )
    ).json()
    assert [m["id"] for m in page["items"]] == [second["id"]]
    assert [m["id"] for m in page["updates"]] == [sent["id"]]
    assert page["updates"][0]["deletedState"] == "by_author"
    assert page["serverTime"].endswith("Z")
    later = (await other.get(base, params={"updatedAfter": page["serverTime"]})).json()
    assert later["updates"] == []
    # 不带 updatedAfter 响应形状不变
    assert set((await other.get(base)).json()) == {
        "items",
        "hasMoreBefore",
        "hasMoreAfter",
    }
    err(
        await other.get(base, params={"updatedAfter": "yesterday"}),
        422,
        "validation_failed",
    )

    # 全部只发到这个交流区频道（没有私信、作品页频道的面板事件）
    assert {c for c in rt.channels() if c.startswith("chat:")} == {channel}
    for c in (plain, other):
        await c.close()


@pytest.mark.asyncio
async def test_restricted_work_broadcast_attachments(env, people, rt):
    """受限作品的交流区按 r18 序列化（R18 图给地址）；全年龄作品不能发 R18 图，广播里的图都是 general。"""
    restricted = await make_work(env, people["author"], "受限", "r18")
    r18 = await client(env, "viewer18")
    img = await make_image(env, people["r18"], "r18")
    res = await r18.post(
        f"/works/{restricted}/chat/messages", {"body": "图", "attachments": [img]}
    )
    assert res.status_code == 201
    await settle()
    event = rt.publishes(f"chat:work.{restricted}", "message.created")[0]
    check_panel(event)
    att = event["message"]["attachments"][0]
    assert att["rating"] == "r18" and att["placeholder"] is False
    await r18.close()


@pytest.mark.asyncio
async def test_dm_publishes_to_both_personal_channels(env, people, rt):
    plain = await client(env, "plain")
    other = await client(env, "other")
    conv = (await plain.post("/conversations", {"userId": people["other"]})).json()
    base = f"/conversations/{conv['id']}/messages"
    sent = (await plain.post(base, {"body": "你好", "nonce": "dm-1"})).json()
    await settle()
    mine = rt.publishes(f"personal:{people['plain']}", "message.created")
    theirs = rt.publishes(f"personal:{people['other']}", "message.created")
    assert len(mine) == 1 and len(theirs) == 1
    for event in (mine[0], theirs[0]):
        check_panel(event)
        assert event["target"] == {"kind": "dm", "id": conv["id"]}
        assert event["message"]["id"] == sent["id"]
    # nonce 只还给发送者
    assert (
        mine[0]["message"]["nonce"] == "dm-1" and theirs[0]["message"]["nonce"] is None
    )
    # 私信没有 chat: 频道
    assert not [c for c in rt.channels() if c.startswith("chat:")]
    # 对方的会话未读数（外层 unread.updated）
    unread = rt.publishes(f"personal:{people['other']}", "unread.updated")
    assert len(unread) == 1
    check_outer(unread[0])
    assert unread[0]["data"] == {"scope": "conv", "id": conv["id"], "count": 1}

    # 对方读了：自己的频道收到 count=0
    res = await other.post(
        f"/conversations/{conv['id']}/read", {"messageId": sent["id"]}
    )
    assert res.status_code == 200
    await settle()
    assert (
        rt.publishes(f"personal:{people['other']}", "unread.updated")[-1]["data"][
            "count"
        ]
        == 0
    )

    # 编辑、删除也推给双方；typing 只推给对方
    await plain.patch(f"{base}/{sent['id']}", {"body": "改"})
    await plain.delete(f"{base}/{sent['id']}")
    await plain.post(f"/conversations/{conv['id']}/typing")
    await settle()
    for user in ("plain", "other"):
        events = rt.publishes(f"personal:{people[user]}", "message.updated")
        assert [e["message"]["deletedState"] for e in events] == [None, "by_author"]
        for e in events:
            check_panel(e)
    assert rt.publishes(f"personal:{people['other']}", "typing")
    assert not rt.publishes(f"personal:{people['plain']}", "typing")
    for c in (plain, other):
        await c.close()


@pytest.mark.asyncio
async def test_publish_failure_does_not_break_requests(env, people, rt):
    rt.fail = True
    work_id = await make_work(env, people["author"])
    plain = await client(env, "plain")
    res = await plain.post(f"/works/{work_id}/chat/messages", {"body": "hi"})
    assert res.status_code == 201
    await settle()
    # Centrifugo 连不上也一样
    publisher.set_transport(
        httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))
        )
    )
    res = await plain.post(f"/works/{work_id}/chat/messages", {"body": "again"})
    assert res.status_code == 201
    await settle()
    await plain.close()


# ---------------- 发布：外层事件 ----------------


@pytest.mark.asyncio
async def test_mention_notification_events(env, people, rt):
    general = await make_work(env, people["author"], "全年龄")
    restricted = await make_work(env, people["author"], "受限", "r18")
    r18 = await client(env, "viewer18")
    personal = f"personal:{people['plain']}"

    # 受限作品里 @ 全年龄模式的人：提醒照记，但不推（R18 隔离）
    res = await r18.post(
        f"/works/{restricted}/chat/messages", {"body": f"<@{people['plain']}> 看这里"}
    )
    assert res.status_code == 201
    await settle()
    assert rt.publishes(personal) == []

    res = await r18.post(
        f"/works/{general}/chat/messages", {"body": f"<@{people['plain']}> 你好"}
    )
    message_id = res.json()["id"]
    await settle()
    created = rt.publishes(personal, "notification.created")
    assert len(created) == 1
    check_outer(created[0])
    data = created[0]["data"]
    assert data["category"] == "mention" and data["context"]["messageId"] == message_id
    s = schemas()
    if s is not None:
        s.component("Notification").validate(data)
    unread = rt.publishes(personal, "unread.updated")
    assert len(unread) == 1
    check_outer(unread[0])
    # 计数只算当前模式看得到的（受限作品那条不算）
    assert unread[0]["data"] == {"scope": "notifications", "id": "mention", "count": 1}
    await r18.close()


@pytest.mark.asyncio
async def test_access_changed_publishes_and_disconnects(env, people, rt):
    r18 = await client(env, "viewer18")
    res = await r18.patch("/me/prefs", {"contentMode": "general"})
    assert res.status_code == 200
    await settle()
    events = rt.publishes(f"personal:{people['r18']}", "access.changed")
    assert len(events) == 1
    check_outer(events[0])
    assert events[0]["data"]["restrictedAccess"]["state"] == "granted"
    # 服务端主动断开（3014：客户端丢掉旧令牌重新取，site_r18 不再在令牌里）
    assert rt.of("disconnect") == [
        {
            "user": people["r18"],
            "disconnect": {"code": 3014, "reason": "access changed"},
        }
    ]
    claims = jwt.decode((await r18.get("/realtime/token")).json()["token"], SECRET)
    assert "site_r18" not in claims["channels"]
    # 只改模糊不推
    rt.calls.clear()
    await r18.patch("/me/prefs", {"blurR18": False})
    await settle()
    assert rt.calls == []
    await r18.close()


@pytest.mark.asyncio
async def test_work_events_and_kick(env, people, rt):
    work_id = await make_work(env, people["author"], "原标题")
    author = await client(env, "author")
    plain = await client(env, "plain")
    # plain 订了作品页（签订阅令牌时记下订阅者）
    assert (
        await plain.get(
            "/realtime/subscribe-token", params={"channel": f"work:{work_id}"}
        )
    ).status_code == 200

    res = await author.patch(f"/works/{work_id}", {"title": "新标题"})
    assert res.status_code == 200, res.text
    await settle()
    events = rt.publishes(f"work:{work_id}", "work.updated")
    assert len(events) == 1
    check_outer(events[0])
    assert events[0]["data"] == {"workId": str(work_id), "fields": ["title"]}

    # 转为受限：work.visibility.changed，并把看不到的订阅者踢出作品页和交流区
    rt.calls.clear()
    async with env.factory() as session:
        work = await session.get(Work, work_id)
        work.is_nsfw = True
        work.visibility = "restricted"
        await session.commit()
    await settle()
    vis = rt.publishes(f"work:{work_id}", "work.visibility.changed")
    assert len(vis) == 1
    check_outer(vis[0])
    assert vis[0]["data"] == {"workId": str(work_id), "visibility": "restricted"}
    updated = rt.publishes(f"work:{work_id}", "work.updated")
    assert set(updated[0]["data"]["fields"]) == {"rating", "visibility"}
    assert sorted(p["channel"] for p in rt.of("unsubscribe")) == sorted(
        [f"work:{work_id}", f"chat:work.{work_id}"]
    )
    assert {p["user"] for p in rt.of("unsubscribe")} == {people["plain"]}
    # 之后再要订阅令牌：404
    err(
        await plain.get(
            "/realtime/subscribe-token", params={"channel": f"work:{work_id}"}
        ),
        404,
        "not_found",
    )
    # 只改 updated_at 之类的列不推
    rt.calls.clear()
    async with env.factory() as session:
        await session.execute(
            update(Work).where(Work.id == work_id).values(updated_at=utc_now())
        )
        work = await session.get(Work, work_id)
        work.last_active_at = utc_now()
        await session.commit()
    await settle()
    assert rt.calls == []
    for c in (author, plain):
        await c.close()


@pytest.mark.asyncio
async def test_mute_and_announcement_events(env, people, rt):
    work_id = await make_work(env, people["author"])
    author = await client(env, "author")
    until = (utc_now() + timedelta(hours=1)).isoformat() + "Z"
    res = await author.post(
        f"/works/{work_id}/mutes", {"userId": people["plain"], "until": until}
    )
    assert res.status_code in (200, 201), res.text
    await settle()
    muted = rt.publishes(f"personal:{people['plain']}", "moderation.muted")
    assert len(muted) == 1
    check_outer(muted[0])
    assert muted[0]["data"]["scope"] == "work" and muted[0]["data"]["workId"] == str(
        work_id
    )
    assert muted[0]["data"]["until"].endswith("Z")

    async with env.factory() as session:
        for audience in ("all", "r18"):
            session.add(
                SiteAnnouncement(
                    audience=audience,
                    kind="normal",
                    title=f"公告 {audience}",
                    body="正文",
                    published_at=utc_now() - timedelta(seconds=1),
                )
            )
        session.add(
            SiteAnnouncement(
                audience="all",
                kind="normal",
                title="定时",
                body="以后才发",
                published_at=utc_now() + timedelta(days=1),
            )
        )
        await session.commit()
    await settle()
    site = rt.publishes("site", "announcement.published")
    site_r18 = rt.publishes("site_r18", "announcement.published")
    assert [e["data"]["title"] for e in site] == ["公告 all"]
    assert [e["data"]["title"] for e in site_r18] == ["公告 r18"]
    for e in site + site_r18:
        check_outer(e)
    await author.close()


@pytest.mark.asyncio
async def test_upload_updated_after_rating(env, people, rt):
    from models.attachment import Attachment

    upload_id = await make_image(env, people["plain"], "general", state="processing")
    async with env.factory() as session:
        att = await session.get(Attachment, int(upload_id))
        att.state = "ready"
        att.rating = "general"
        await session.commit()
    await settle()
    events = rt.publishes(f"personal:{people['plain']}", "upload.updated")
    assert len(events) == 1
    check_outer(events[0])
    assert (
        events[0]["data"]["id"] == upload_id and events[0]["data"]["state"] == "ready"
    )
