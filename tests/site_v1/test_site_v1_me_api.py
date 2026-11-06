"""/api/v1 第二期 me 块：我的、提醒、公告规则、作品内（确认/规则/钉选/关注/禁言）、书单、好友。

环境和 test_site_v1_works_api.py 相同（FakeFluxer + create_all 的测试库）。
"""

import pytest
from sqlalchemy import select

from models.site_me import SiteAnnouncement, SiteNotification, SiteRuleDoc
from models.thread_follow import ThreadFollow

from test_site_v1_session_api import assert_error
from test_site_v1_works_api import _login, _published, env  # noqa: F401


@pytest.mark.asyncio
async def test_ack_rules_pins_follow(env):  # noqa: F811
    app, account, tag_id, factory = env
    await account("author", access="granted")
    await account("reader")
    author = await _login(app, "author")
    reader = await _login(app, "reader")
    work = await _published(author, tag_id, "作品 A")
    wid = work["id"]

    # 没有公告、规则、版本时都不用确认
    w = (await reader.get(f"/works/{wid}")).json()
    assert w["viewer"]["needsAck"] == {
        "announcement": False,
        "rules": False,
        "version": False,
    }

    # 关注后作者发公告、改规则：reader 收到提醒，needsAck 变 true
    assert (await reader.write("POST", f"/works/{wid}/follow")).status_code == 204
    ann = await author.write(
        "POST", f"/works/{wid}/announcements", {"title": "公告", "body": "内容"}
    )
    assert ann.status_code == 201 and ann.json()["version"] == 1
    rules = await author.write("PUT", f"/works/{wid}/rules", {"body": "规则一"})
    assert rules.status_code == 200 and rules.json()["version"] == 2
    assert_error(
        await reader.write("PUT", f"/works/{wid}/rules", {"body": "x"}),
        403,
        "forbidden",
    )

    w = (await reader.get(f"/works/{wid}")).json()
    assert w["viewer"]["followed"] is True
    assert w["viewer"]["needsAck"]["announcement"] and w["viewer"]["needsAck"]["rules"]
    # 作者自己不用确认
    assert not any(
        (await author.get(f"/works/{wid}")).json()["viewer"]["needsAck"].values()
    )

    counts = (await reader.get("/notifications/unread-counts")).json()
    assert counts["categories"].get("work_update") == 2 and counts["total"] == 2
    page = (
        await reader.get("/notifications", params={"category": "work_update"})
    ).json()
    assert len(page["items"]) == 2 and all(
        i["target"]["id"] == wid for i in page["items"]
    )

    # 确认
    for kind, version in (("announcement", 1), ("rules", "2")):
        res = await reader.write(
            "POST", f"/works/{wid}/ack", {"kind": kind, "version": version}
        )
        assert res.status_code == 204
    assert_error(
        await reader.write("POST", f"/works/{wid}/ack", {"kind": "x", "version": 1}),
        422,
        "validation_failed",
    )
    w = (await reader.get(f"/works/{wid}")).json()
    assert (
        not w["viewer"]["needsAck"]["announcement"]
        and not w["viewer"]["needsAck"]["rules"]
    )

    # 关注列表：有更新；标记已看后没有
    follows = (await reader.get("/me/follows")).json()["items"]
    assert follows[0]["work"]["id"] == wid
    assert (
        await reader.write("POST", "/me/follows/mark-viewed", {})
    ).status_code == 204
    assert (await reader.get("/me/follows", params={"onlyUpdated": "1"})).json()[
        "items"
    ] == []

    # 全部已读
    res = await reader.write("POST", "/notifications/read", {})
    assert res.status_code == 200 and res.json()["total"] == 0

    # 钉选
    pin = await author.write(
        "POST", f"/works/{wid}/pins", {"messageId": "m1", "excerpt": "钉"}
    )
    assert pin.status_code == 201
    assert_error(
        await reader.write("POST", f"/works/{wid}/pins", {"excerpt": "x"}),
        403,
        "forbidden",
    )
    ids = [p["id"] for p in (await reader.get(f"/works/{wid}/pins")).json()["items"]]
    assert pin.json()["id"] in ids
    assert (
        await author.write("DELETE", f"/works/{wid}/pins/{pin.json()['id']}")
    ).status_code == 204
    assert (await reader.get(f"/works/{wid}/pins")).json()["items"] == []

    # 取消关注
    assert (await reader.write("DELETE", f"/works/{wid}/follow")).status_code == 204
    async with factory() as session:
        row = (await session.execute(select(ThreadFollow))).scalars().first()
        assert row.active_flag is False and row.user_id is None


@pytest.mark.asyncio
async def test_r18_notifications_hidden_in_general(env):  # noqa: F811
    app, account, tag_id, factory = env
    author_id = await account("author", access="granted")
    reader_id = await account("reader", access="granted")
    author = await _login(app, "author")
    reader = await _login(app, "reader")
    await author.write("PATCH", "/me/prefs", {"contentMode": "r18"})
    work = await _published(author, tag_id, "受限作品", rating="R18")
    async with factory() as session:
        session.add(
            SiteNotification(
                user_id=reader_id,
                category="work_update",
                actor_id=author_id,
                target={"type": "work", "id": work["id"], "title": "受限作品"},
                excerpt="更新",
                work_id=int(work["id"]),
            )
        )
        await session.commit()
    assert (await reader.get("/notifications/unread-counts")).json()["total"] == 0
    assert (await reader.get("/notifications")).json()["items"] == []
    await reader.write("PATCH", "/me/prefs", {"contentMode": "r18"})
    assert (await reader.get("/notifications/unread-counts")).json()["total"] == 1


@pytest.mark.asyncio
async def test_announcements_and_rules(env):  # noqa: F811
    app, account, tag_id, factory = env
    await account("reader", access="granted")
    reader = await _login(app, "reader")
    async with factory() as session:
        session.add(
            SiteAnnouncement(title="规则", kind="rule", requires_ack=True, body="b")
        )
        session.add(
            SiteAnnouncement(
                title="R18", audience="r18", kind="rule", requires_ack=True, body="b"
            )
        )
        session.add(SiteRuleDoc(slug="faq", title="常见问题", body="正文"))
        await session.commit()
    pending = (await reader.get("/announcements/pending-ack")).json()
    assert [a["title"] for a in pending] == ["规则"]
    assert (
        await reader.write("POST", f"/announcements/{pending[0]['id']}/ack")
    ).status_code == 204
    assert (await reader.get("/announcements/pending-ack")).json() == []
    assert len((await reader.get("/announcements")).json()["items"]) == 1
    # 改版本后要重新确认
    async with factory() as session:
        row = await session.get(SiteAnnouncement, int(pending[0]["id"]))
        row.version = 2
        await session.commit()
    assert len((await reader.get("/announcements/pending-ack")).json()) == 1
    rules = (await reader.get("/rules")).json()["items"]
    assert rules[0]["slug"] == "faq" and "body" not in rules[0]
    assert (await reader.get("/rules/faq")).json()["body"] == "正文"
    assert_error(await reader.get("/rules/none"), 404, "not_found")


@pytest.mark.asyncio
async def test_collections(env):  # noqa: F811
    app, account, tag_id, factory = env
    await account("author", access="granted")
    await account("reader")
    author = await _login(app, "author")
    reader = await _login(app, "reader")
    w1 = await _published(author, tag_id, "全年龄")
    await author.write("PATCH", "/me/prefs", {"contentMode": "r18"})
    w2 = await _published(author, tag_id, "受限", rating="R18")

    res = await author.write(
        "POST", "/collections", {"title": "书单", "defaultSort": "order"}
    )
    assert res.status_code == 201
    cid = res.json()["id"]
    assert (
        await author.write(
            "POST", f"/collections/{cid}/items", {"workId": w1["id"], "note": "好"}
        )
    ).status_code == 201
    sync = await author.write(
        "POST", "/collections/sync", {"workId": w2["id"], "add": [cid]}
    )
    assert sync.json()["added"] == [cid]
    # 受限作品对全年龄的 reader 不返回、不计数
    detail = (await reader.get(f"/collections/{cid}")).json()
    assert detail["itemCount"] == 1 and detail["viewer"]["canEdit"] is False
    items = (await reader.get(f"/collections/{cid}/items")).json()["items"]
    assert [i["work"]["id"] for i in items] == [w1["id"]] and items[0]["note"] == "好"
    assert (await author.get(f"/collections/{cid}")).json()["itemCount"] == 2
    # 只有创建者能改
    assert_error(
        await reader.write("PATCH", f"/collections/{cid}", {"title": "x"}),
        403,
        "forbidden",
    )
    patched = await author.write(
        "PATCH", f"/collections/{cid}/items/{w1['id']}", {"note": "很好", "order": 9}
    )
    assert patched.json()["note"] == "很好" and patched.json()["order"] == 9
    # 收藏书单
    fav = await reader.write("POST", f"/collections/{cid}/favorite")
    assert fav.json()["viewer"]["favorited"] and fav.json()["favoriteCount"] == 1
    mine = (await reader.get("/me/collections", params={"kind": "favorited"})).json()[
        "items"
    ]
    assert [c["id"] for c in mine] == [cid]
    own = (
        await author.get("/me/collections", params={"containsWork": w1["id"]})
    ).json()["items"]
    assert own[0]["containsWork"] is True
    # 作品收藏（默认书单）不算书单
    await author.write("POST", f"/works/{w1['id']}/favorite")
    assert len((await author.get("/me/collections")).json()["items"]) == 1
    assert (await author.get("/me/favorites")).json()["items"][0]["work"]["id"] == w1[
        "id"
    ]
    # 私密书单别人 404
    await author.write("PATCH", f"/collections/{cid}", {"isPublic": False})
    assert_error(await reader.get(f"/collections/{cid}"), 404, "not_found")
    assert (await author.write("DELETE", f"/collections/{cid}")).status_code == 204
    assert_error(await author.get(f"/collections/{cid}"), 404, "not_found")


@pytest.mark.asyncio
async def test_friends(env):  # noqa: F811
    app, account, tag_id, factory = env
    a_id = await account("alice")
    b_id = await account("bob")
    alice = await _login(app, "alice")
    bob = await _login(app, "bob")
    req = await alice.write("POST", "/friends/requests", {"userId": "bob"})
    assert req.status_code == 201
    assert_error(
        await bob.write("POST", "/friends/requests", {"userId": "alice"}),
        409,
        "conflict",
    )
    profile = (await bob.get(f"/users/{a_id}")).json()
    assert profile["viewer"]["friendRequest"]["direction"] == "incoming"
    incoming = (await bob.get("/friends/requests")).json()["incoming"]
    assert incoming[0]["from"]["id"] == a_id
    assert (
        await bob.write("POST", "/friends", {"requestId": incoming[0]["id"]})
    ).status_code == 201
    assert (await alice.get("/friends")).json()["items"][0]["user"]["id"] == b_id
    assert (await bob.get(f"/users/{a_id}")).json()["viewer"]["friend"] is True
    notes = (
        await alice.get("/notifications", params={"category": "interaction"})
    ).json()["items"]
    assert notes[0]["excerpt"] == "通过了你的好友申请"
    assert (await alice.write("DELETE", f"/friends/{b_id}")).status_code == 204
    assert (await alice.get("/friends")).json()["items"] == []


@pytest.mark.asyncio
async def test_me_documents(env):  # noqa: F811
    app, account, tag_id, factory = env
    await account("author", access="granted")
    author = await _login(app, "author")
    w = await _published(author, tag_id, "精选")
    res = await author.write(
        "PUT", "/me/preferences", {"baseVersion": 0, "data": {"display": {"a": 1}}}
    )
    assert res.json()["version"] == 1
    conflict = await author.write(
        "PUT", "/me/preferences", {"baseVersion": 0, "data": {}}
    )
    assert_error(conflict, 409, "conflict")
    assert conflict.json()["error"]["details"]["current"]["version"] == 1
    habits = await author.write(
        "PUT",
        "/me/tag-habits",
        {
            "entries": [
                {"name": "古风", "score": 1.5, "updatedAt": "2026-11-01T00:00:00Z"}
            ],
            "hidden": ["a"],
        },
    )
    assert habits.status_code == 200 and habits.json()["entries"][0]["tagId"] is None
    assert (await author.write("DELETE", "/me/tag-habits")).status_code == 204
    assert (await author.get("/me/tag-habits")).json()["updatedAt"] is None
    pinned = await author.write(
        "PUT", "/me/pinned-works", {"items": [{"workId": w["id"], "note": "好"}]}
    )
    assert pinned.json()[0]["work"]["id"] == w["id"]
    profile = (await Client_anon(app).get(f"/users/{w['author']['id']}")).json()
    assert profile["pinnedWorks"][0]["workId"] == w["id"]
    tok = await author.write("POST", "/me/plugin-tokens", {"name": "酒馆"})
    assert tok.status_code == 201 and tok.json()["secret"].startswith("odp_")
    assert tok.json()["secret"] not in (await author.get("/me/plugin-tokens")).text
    deco = (await author.get("/me/decorations")).json()
    assert deco["decorationRoles"] == [] and deco["showcase"] == []
    assert_error(
        await author.write("PATCH", "/me/decorations", {"showcase": ["x"]}),
        422,
        "validation_failed",
    )
    assert (await author.get("/me/works")).json()["items"][0]["id"] == w["id"]


def Client_anon(app):
    from test_site_v1_session_api import Client

    return Client(app)


@pytest.mark.asyncio
async def test_create_notification_for_other_blocks(env):  # noqa: F811
    from site_v1.services.notifications import create_notification

    app, account, tag_id, factory = env
    a_id = await account("alice")
    await account("bob")
    bob_id = await account("bob")
    bob = await _login(app, "bob")
    async with factory() as session:
        create_notification(
            session,
            bob_id,
            {
                "category": "mention",
                "actor": {"id": a_id},
                "target": {"type": "work", "id": "999", "title": "x"},
                "excerpt": "@bob",
                "mention": {"scope": "user"},
            },
        )
        create_notification(
            session,
            bob_id,
            {"category": "mention", "actorId": a_id, "target": {"type": "user", "id": a_id, "title": "a"}, "excerpt": "hi"},
        )
        await session.commit()
    # 作品 999 不存在（看不到）：不返回、不计数
    items = (await bob.get("/notifications")).json()["items"]
    assert [i["excerpt"] for i in items] == ["hi"]
    assert (await bob.get("/notifications/unread-counts")).json()["categories"] == {"mention": 1}
