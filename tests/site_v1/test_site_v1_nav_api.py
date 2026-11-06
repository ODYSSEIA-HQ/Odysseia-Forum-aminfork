# ruff: noqa: F811  （env 夹具从 test_site_v1_works_api 导入复用）
"""/api/v1 找作品一块（nav）：首页、主榜概览、搜索联想、横幅、tag 浏览与治理、首页配置、频道、旧帖跳转。

夹具同 test_site_v1_works_api.py（FakeFluxer + create_all 测试库 + tag 目录种子）。
"""

import pytest
from sqlalchemy import select

from models.site_tag import SiteWorkTag
from models.thread import Thread
from models.work import Work

from test_site_v1_session_api import Client, assert_error
from test_site_v1_works_api import _add, _login, _published, env  # noqa: F401

ALL = ("site.admin",)


async def _r18(c):
    res = await c.write("PATCH", "/me/prefs", {"contentMode": "r18"})
    assert res.status_code == 200, res.text


async def _setup(env):
    app, account, tag_id, factory = env
    await account("author", access="granted")
    await account("staff", access="granted", permissions=ALL)
    await account("viewer")
    author = await _login(app, "author")
    pub = await _published(author, tag_id, "全年龄作品甲", type_name="美化")
    await _add(author, pub["id"], {"name": "自由测试"})
    await _r18(author)
    r18 = await _published(author, tag_id, "R18作品乙", rating="R18", type_name="美化")
    await _add(author, r18["id"], {"name": "只在受限"})
    return app, author, pub, r18, tag_id, factory


@pytest.mark.asyncio
async def test_home_overview_suggest_filter_r18(env):
    app, author, pub, r18, tag_id, _ = await _setup(env)
    anon = Client(app)
    home = (await anon.get("/home")).json()
    ids = {w["id"] for rail in home["rails"] for w in rail["items"]}
    assert pub["id"] in ids and r18["id"] not in ids
    assert all(rail["items"] for rail in home["rails"])
    assert {"banners", "rails", "featured", "promotions"} <= set(home)
    featured = (await anon.get("/home/featured")).json()["items"]
    assert featured and all(x["work"]["id"] != r18["id"] for x in featured)

    ov = (await anon.get("/boards/overview")).json()["items"]
    card = next(x for x in ov if x["board"]["title"] == "美化")
    assert card["workCount"] == 1 and [w["id"] for w in card["top"]] == [pub["id"]]
    ov18 = (await author.get("/boards/overview")).json()["items"]
    assert next(x for x in ov18 if x["board"]["title"] == "美化")["workCount"] == 2

    sug = (await anon.get("/search/suggest", params={"q": "作品"})).json()
    assert [w["id"] for w in sug["works"]] == [pub["id"]]
    assert set(sug) == {"tags", "works", "users", "lists", "collections", "events"}
    assert (await anon.get("/search/suggest", params={"q": "只在受限"})).json()[
        "tags"
    ] == []
    assert (await author.get("/search/suggest", params={"q": "只在受限"})).json()[
        "tags"
    ][0]["count"] == 1


@pytest.mark.asyncio
async def test_tag_directory_detail_follow(env):
    app, author, pub, r18, tag_id, _ = await _setup(env)
    anon = Client(app)
    items = (await anon.get("/tags", params={"group": "free"})).json()["items"]
    assert [t["name"] for t in items] == ["自由测试"]
    assert_error(
        await anon.get("/tags", params={"group": "nope"}), 422, "validation_failed"
    )
    restricted = next(
        t
        for t in (await author.get("/tags", params={"group": "free"})).json()["items"]
        if t["name"] == "只在受限"
    )
    # 只出现在受限作品上的 tag：全年龄模式 404
    assert_error(await anon.get(f"/tags/{restricted['id']}"), 404, "not_found")
    assert (await author.get(f"/tags/{restricted['id']}")).status_code == 200
    card = await tag_id("type", "美化")
    detail = (await anon.get(f"/tags/{card}")).json()
    assert detail["workCount"] == 1 and detail["typeCounts"] == [
        {"type": "美化", "count": 1}
    ]
    assert any(t["name"] == "自由测试" for t in detail["related"])

    me = (await author.write("POST", f"/tags/{card}/follow")).json()
    assert card in me["prefs"]["followedTags"]
    me = (await author.write("POST", f"/tags/{card}/block")).json()
    assert (
        card in me["prefs"]["blockedTags"] and card not in me["prefs"]["followedTags"]
    )
    me = (await author.write("DELETE", f"/tags/{card}/block")).json()
    assert card not in me["prefs"]["blockedTags"]
    assert_error(
        await anon.write("POST", f"/tags/{card}/follow"), 401, "unauthenticated"
    )


@pytest.mark.asyncio
async def test_tag_lock_and_log(env):
    app, author, pub, r18, tag_id, _ = await _setup(env)
    viewer = await _login(app, "viewer")
    assert_error(
        await viewer.write("PUT", f"/works/{pub['id']}/tag-lock", {"locked": True}),
        403,
        "forbidden",
    )
    res = await author.write("PUT", f"/works/{pub['id']}/tag-lock", {"locked": True})
    assert res.status_code == 200 and res.json()["tagLocked"] is True
    assert_error(
        await author.write("PUT", f"/works/{pub['id']}/tag-lock", {}),
        422,
        "validation_failed",
    )
    log = (await author.get(f"/works/{pub['id']}/tag-log")).json()["items"]
    assert {e["tagName"] for e in log} >= {"全年龄", "美化", "自由测试"}
    assert all(e["action"] == "add" for e in log)
    assert_error(await viewer.get(f"/works/{pub['id']}/tag-log"), 403, "forbidden")


@pytest.mark.asyncio
async def test_admin_tags(env):
    app, author, pub, r18, tag_id, factory = await _setup(env)
    staff = await _login(app, "staff")
    assert_error(
        await author.write("POST", "/admin/tags", {"group": "theme", "name": "x"}),
        403,
        "forbidden",
    )
    res = await staff.write(
        "POST",
        "/admin/tags",
        {"group": "theme", "name": "新题材", "requiredQuestionFor": ["角色卡"]},
    )
    assert res.status_code == 201, res.text
    new = res.json()["tag"]
    assert new["official"] and new["requiredQuestionFor"] == ["角色卡"]
    assert_error(
        await staff.write("POST", "/admin/tags", {"group": "theme", "name": "新题材"}),
        422,
        "validation_failed",
    )
    res = await staff.write(
        "POST", f"/admin/tags/{new['id']}/synonyms", {"names": ["新题", "NT"]}
    )
    assert res.json()["aliases"] == ["新题", "NT"]
    # 打 tag 时同义词归到主 tag
    work = await _add(author, pub["id"], {"name": "新题"})
    assert any(t["id"] == new["id"] for t in work["tags"])

    free = next(t for t in work["tags"] if t["name"] == "自由测试")
    res = await staff.write(
        "POST", f"/admin/tags/{free['id']}/promote", {"group": "attribute"}
    )
    assert (
        res.status_code == 200
        and res.json()["tag"]["official"]
        and res.json()["affectedWorks"] == 1
    )
    res = await staff.write(
        "POST", f"/admin/tags/{free['id']}/merge", {"targetId": new["id"]}
    )
    assert res.status_code == 200 and "自由测试" in res.json()["tag"]["aliases"]
    res = await staff.write(
        "POST", f"/admin/tags/{new['id']}/disable", {"reason": "测试"}
    )
    assert res.json() == {"tag": None, "affectedWorks": 1}
    assert_error(
        await author.write("POST", f"/works/{pub['id']}/tags", {"name": "新题材"}),
        422,
        "validation_failed",
    )
    async with factory() as session:
        active = (
            (
                await session.execute(
                    select(SiteWorkTag).where(
                        SiteWorkTag.work_id == int(pub["id"]),
                        SiteWorkTag.removed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert int(new["id"]) not in {r.tag_id for r in active}
    rating = await tag_id("rating", "全年龄")
    assert_error(
        await staff.write("POST", f"/admin/tags/{rating}/disable", {"reason": "x"}),
        422,
        "validation_failed",
    )
    assert (await staff.get("/admin/tags/promote-candidates")).status_code == 200


@pytest.mark.asyncio
async def test_home_admin_and_banners(env):
    app, author, pub, r18, tag_id, factory = await _setup(env)
    staff = await _login(app, "staff")
    assert_error(await author.get("/admin/home"), 403, "forbidden")
    rails = (await staff.get("/admin/home")).json()["rails"]
    res = await staff.write(
        "PUT",
        "/admin/home",
        {
            "rails": [
                {
                    "kind": "curated",
                    "refId": None,
                    "title": "手挑",
                    "order": 0,
                    "workIds": [pub["id"], r18["id"]],
                }
            ]
        },
    )
    assert res.status_code == 200, res.text
    home = (await Client(app).get("/home")).json()
    assert [r["kind"] for r in home["rails"]] == ["curated"] and [
        w["id"] for w in home["rails"][0]["items"]
    ] == [pub["id"]]
    assert len(rails) == 5
    res = await staff.write(
        "PUT",
        "/admin/home/featured",
        {
            "editorPicks": [pub["id"]],
            "promotions": [
                {
                    "kind": "work",
                    "targetId": r18["id"],
                    "placement": "both",
                    "startAt": "2020-01-01T00:00:00Z",
                    "endAt": "2020-01-10T00:00:00Z",
                    "order": 0,
                    "weight": 5,
                }
            ],
        },
    )
    assert res.status_code == 200 and res.json()["promotions"][0]["state"] == "expired"

    # 横幅：作者申请 → 站务通过 → 全站横幅
    res = await author.write(
        "POST",
        "/banners/applications",
        {"workId": pub["id"], "scope": "global", "note": "n"},
    )
    assert res.status_code == 201, res.text
    app_id = res.json()["id"]
    assert_error(
        await author.write("POST", "/banners/applications", {"workId": pub["id"]}),
        409,
        "conflict",
    )
    pending = (await staff.get("/admin/banners/applications")).json()["items"]
    assert [a["id"] for a in pending] == [app_id]
    res = await staff.write(
        "POST",
        f"/admin/banners/applications/{app_id}/resolve",
        {
            "decision": "approve",
            "startAt": "2020-01-01T00:00:00Z",
            "endAt": "2020-01-10T00:00:00Z",
            "reason": "",
        },
    )
    # 作品没有封面也没另传横幅图：不能通过
    assert_error(res, 422, "validation_failed")
    res = await staff.write(
        "POST",
        f"/admin/banners/applications/{app_id}/resolve",
        {"decision": "reject", "reason": "换图"},
    )
    assert res.status_code == 200 and res.json()["state"] == "rejected"
    mine = (await author.get("/banners/applications")).json()["items"]
    assert mine[0]["resolution"]["reason"] == "换图"
    assert (await Client(app).get("/banners")).json() == {"items": []}


@pytest.mark.asyncio
async def test_channels(env):
    app, author, pub, r18, tag_id, _ = await _setup(env)
    staff = await _login(app, "staff")
    assert_error(
        await author.write("POST", "/channels", {"name": "x", "rating": "general"}),
        403,
        "forbidden",
    )
    a = (
        await staff.write(
            "POST", "/channels", {"id": "chat-a", "name": "甲", "rating": "general"}
        )
    ).json()
    b = await staff.write(
        "POST", "/channels", {"name": "乙", "rating": "r18", "rules": "规则"}
    )
    assert b.status_code == 201
    b = b.json()
    assert_error(
        await staff.write(
            "POST", "/channels", {"id": "chat-a", "name": "x", "rating": "general"}
        ),
        409,
        "conflict",
    )
    assert [c["id"] for c in (await Client(app).get("/channels")).json()] == ["chat-a"]
    await _r18(staff)
    res = await staff.write(
        "PATCH", f"/channels/{b['id']}", {"order": 0, "name": "乙二"}
    )
    assert res.json()["order"] == 0 and res.json()["name"] == "乙二"
    assert [c["id"] for c in (await staff.get("/channels")).json()] == [
        b["id"],
        "chat-a",
    ]
    assert (await staff.write("DELETE", f"/channels/{a['id']}")).status_code == 204
    assert [c["order"] for c in (await staff.get("/admin/channels")).json()] == [0]


@pytest.mark.asyncio
async def test_by_legacy_discord(env):
    app, author, pub, r18, tag_id, factory = await _setup(env)
    async with factory() as session:
        work = await session.get(Work, int(pub["id"]))
        session.add(
            Thread(
                platform="discord",
                work_id=work.id,
                role="main",
                guild_id=1,
                channel_id=2,
                thread_id=777,
                title="t",
            )
        )
        r18w = await session.get(Work, int(r18["id"]))
        r18w.source_platform, r18w.source_thread_id = "fluxer", ""
        await session.commit()
    anon = Client(app)
    assert (await anon.get("/works/by-legacy/discord/777")).json() == {
        "workId": pub["id"]
    }
    assert_error(await anon.get("/works/by-legacy/discord/778"), 404, "not_found")
