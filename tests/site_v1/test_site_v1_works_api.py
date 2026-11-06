"""/api/v1 作品、收藏、tag、tag 目录、他人作品。

和 test_site_v1_session_api.py 一样：FakeFluxer + create_all 的测试库（TEST_DB_URL），
每个用例新建一个只挂 /api/v1 的 app；tag 目录用 scripts/seed_site_v1.py 的 seed_tag_catalog 写入。
"""

import importlib.util
import os

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from models.attachment import Attachment
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.site_tag import SiteTag
from models.work import Work
from shared.time_utils import utc_now
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.services import works as works_service
from site_v1.services.accounts import ensure_account_rows

from test_site_v1_session_api import PASSWORD, Client, assert_error

_SEED = os.path.join(os.path.dirname(__file__), "../../scripts/seed_site_v1.py")


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_site_v1", _SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def env(monkeypatch, db_session_factory, redis_client):
    monkeypatch.setenv("FLUXER_FAKE", "1")
    monkeypatch.setenv("SITE_REGISTRATION_MODE", "open")
    monkeypatch.delenv("SITE_COOKIE_SECURE", raising=False)
    reload_config()
    set_session_factory(db_session_factory)
    fake = FakeFluxer(session_factory=db_session_factory, redis=redis_client)
    set_fluxer(fake)
    works_service.reset_hot_cache()
    await _seed_module().seed_tag_catalog(db_session_factory)

    from site_v1 import install

    app = FastAPI()
    install(app)

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

    async def tag_id(group, name):
        async with db_session_factory() as session:
            return str(
                (
                    await session.execute(
                        select(SiteTag.id).where(
                            SiteTag.group_id == group, SiteTag.name == name
                        )
                    )
                ).scalar_one()
            )

    yield app, account, tag_id, db_session_factory
    set_session_factory(None)
    set_fluxer(None)
    for var in ("FLUXER_FAKE", "SITE_REGISTRATION_MODE"):
        monkeypatch.delenv(var, raising=False)
    reload_config()


async def _login(app, username):
    c = Client(app)
    res = await c.login(username)
    assert res.status_code == 200, res.text
    return c


async def _draft(c, title="作品"):
    res = await c.write("POST", "/works", {"title": title, "summary": "简介"})
    assert res.status_code == 201, res.text
    return res.json()


async def _add(c, work_id, body, status=(200, 201)):
    res = await c.write("POST", f"/works/{work_id}/tags", body)
    assert res.status_code in status, res.text
    return res.json()


async def _publish(c, work_id, answers=(), terms="2026-10"):
    return await c.write(
        "POST",
        f"/works/{work_id}/publish",
        {"acknowledgedTermsVersion": terms, "answers": list(answers)},
    )


async def _published(c, tag_id, title, rating="全年龄", type_name="美化"):
    w = await _draft(c, title)
    await _add(c, w["id"], {"tagId": await tag_id("rating", rating)})
    await _add(c, w["id"], {"tagId": await tag_id("type", type_name)})
    res = await _publish(c, w["id"])
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.asyncio
async def test_catalog_shape(env):
    app, *_ = env
    c = Client(app)
    res = await c.get("/tags/catalog")
    assert res.status_code == 200
    body = res.json()
    assert body["maxTags"] == 15 and body["requiredQuestions"][0]["id"] == "jailbreak"
    groups = {g["id"]: g for g in body["groups"]}
    assert groups["rating"]["required"] and groups["rating"]["maxSelect"] == 1
    assert {t["rating"] for t in groups["rating"]["tags"]} == {"general", "r18", "r18g"}
    assert groups["audience"]["requiredForTypes"] == ["角色卡"]
    assert all("rating" not in t for t in groups["type"]["tags"])


@pytest.mark.asyncio
async def test_draft_publish_and_visibility(env):
    app, account, tag_id, factory = env
    await account("author", access="granted")
    await account("reader")
    author = await _login(app, "author")
    reader = await _login(app, "reader")
    anon = Client(app)

    draft = await _draft(author, "草稿")
    assert draft["status"] == "draft" and draft["viewer"]["role"] == "author"
    assert_error(await reader.get(f"/works/{draft['id']}"), 404, "not_found")
    # 草稿在 V2 也看不到：source_visible=false，且没有立刻建原生帖子
    async with factory() as session:
        row = await session.get(Work, int(draft["id"]))
        assert row.source_visible is False and row.source_platform == "fluxer"

    # PATCH 不能带 rating；能改说明
    assert_error(
        await author.write("PATCH", f"/works/{draft['id']}", {"rating": "r18"}),
        422,
        "validation_failed",
    )
    patched = await author.write(
        "PATCH", f"/works/{draft['id']}", {"description": "## 说明", "layout": {"a": 1}}
    )
    assert patched.status_code == 200 and patched.json()["description"] == "## 说明"
    assert_error(
        await reader.write("PATCH", f"/works/{draft['id']}", {"summary": "x"}),
        404,
        "not_found",
    )

    # 缺必选组、没勾承诺
    err = assert_error(
        await _publish(author, draft["id"], terms="old"), 422, "validation_failed"
    )
    assert {"tags", "acknowledgedTermsVersion"} <= set(err["details"]["fields"])

    await _add(author, draft["id"], {"tagId": await tag_id("rating", "全年龄")})
    await _add(author, draft["id"], {"tagId": await tag_id("type", "美化")})
    res = await _publish(author, draft["id"])
    assert res.status_code == 200, res.text
    work = res.json()
    assert work["status"] == "published" and work["visibility"] == "public"
    assert set(work["stats"]) == {"favorites", "downloads", "chatMessages"}
    async with factory() as session:
        assert (await session.get(Work, int(work["id"]))).source_visible is True
    assert_error(await _publish(author, draft["id"]), 409, "conflict")

    page = (await anon.get("/works", params={"sort": "latest"})).json()
    assert work["id"] in [w["id"] for w in page["items"]]
    assert (await reader.get(f"/works/{work['id']}")).json()["viewer"]["role"] == "none"
    assert_error(
        await reader.write("PATCH", f"/works/{work['id']}", {"summary": "x"}),
        403,
        "forbidden",
    )

    # 他人作品
    mine = (await anon.get(f"/users/{work['author']['id']}/works")).json()
    assert [w["id"] for w in mine["items"]] == [work["id"]]

    # 软删除
    assert (await author.write("DELETE", f"/works/{work['id']}")).status_code == 204
    assert_error(await author.get(f"/works/{work['id']}"), 404, "not_found")


@pytest.mark.asyncio
async def test_r18_isolation(env):
    app, account, tag_id, factory = env
    author_id = await account("author", access="granted")
    await account("reader")
    author = await _login(app, "author")
    reader = await _login(app, "reader")
    general = await _published(author, tag_id, "全年龄作品")
    r18 = await _published(author, tag_id, "R18 作品", rating="R18")
    r18g = await _published(author, tag_id, "R18G 作品", rating="R18G")
    assert r18["visibility"] == "restricted" and r18["rating"] == "r18"
    async with factory() as session:
        row = await session.get(Work, int(r18g["id"]))
        assert (row.is_nsfw, row.is_abyss, row.visibility) == (True, True, "restricted")

    for c in (Client(app), reader, author):  # author 现在是 general 模式
        res = await c.get(f"/works/{r18['id']}")
        assert_error(res, 404, "not_found")
        assert res.headers["X-Content-Mode"] == "general"
        page = (
            await c.get("/works", params={"rating": "r18,r18g", "limit": 100})
        ).json()
        assert page["items"] == []

    res = await author.write("PATCH", "/me/prefs", {"contentMode": "r18"})
    assert res.status_code == 200, res.text
    res = await author.get(f"/works/{r18['id']}")
    assert res.status_code == 200 and res.headers["X-Content-Mode"] == "r18"
    ids = [
        w["id"]
        for w in (await author.get("/works", params={"sort": "new"})).json()["items"]
    ]
    assert general["id"] in ids and r18["id"] in ids and r18g["id"] not in ids
    await author.write("PATCH", "/me/prefs", {"showR18G": True})
    ids = [
        w["id"]
        for w in (await author.get("/works", params={"sort": "new"})).json()["items"]
    ]
    assert r18g["id"] in ids
    only = (await author.get("/works", params={"rating": "general"})).json()["items"]
    assert [w["id"] for w in only] == [general["id"]]
    users = (await reader.get(f"/users/{author_id}/works")).json()["items"]
    assert [w["id"] for w in users] == [general["id"]]

    # 屏蔽 tag：prefs.blockedTags 里的 tag 不出现
    async with factory() as session:
        prefs = await session.get(SiteUserPrefs, author_id)
        prefs.blocked_tag_ids = [await tag_id("type", "美化")]
        await session.commit()
    assert (await author.get("/works")).json()["items"] == []

    # 搜索、联想、榜单、相似、抽卡
    found = (await reader.get("/search", params={"q": "作品"})).json()["items"]
    assert [w["id"] for w in found] == [general["id"]]
    suggest = (await reader.get("/tags/suggest", params={"q": "美化"})).json()["items"]
    assert suggest and suggest[0]["count"] == 1
    boards = (await reader.get("/boards")).json()["items"]
    assert boards[0]["id"] == "all"
    board = (await reader.get("/boards/all", params={"rating": "r18"})).json()
    assert board["items"] == []
    assert (await reader.get(f"/works/{general['id']}/similar")).json()["items"] == []
    random_items = (await reader.get("/works/random")).json()["items"]
    assert [w["id"] for w in random_items] == [general["id"]]


@pytest.mark.asyncio
async def test_tag_rules(env):
    app, account, tag_id, _ = env
    await account("author", access="granted")
    await account("newbie", newcomer=True)
    await account("other")
    author = await _login(app, "author")
    newbie = await _login(app, "newbie")
    other = await _login(app, "other")

    w = await _draft(author)
    await _add(author, w["id"], {"tagId": await tag_id("rating", "全年龄")})
    w2 = await _add(author, w["id"], {"tagId": await tag_id("rating", "R18")})
    assert [t["name"] for t in w2["tags"] if t["group"] == "rating"] == ["R18"]
    assert w2["rating"] == "r18" and w2["visibility"] == "restricted"
    free = await _add(author, w["id"], {"name": "自由标签"})
    tag = next(t for t in free["tags"] if t["group"] == "free")
    assert tag["official"] is False and tag["addedBy"]
    # 重复加：200 不变
    res = await author.write("POST", f"/works/{w['id']}/tags", {"name": "自由标签"})
    assert res.status_code == 200
    assert_error(
        await author.write("POST", f"/works/{w['id']}/tags", {"name": "有 空格"}),
        422,
        "validation_failed",
    )
    for i in range(13):
        await _add(author, w["id"], {"name": f"标签{i}"})
    assert_error(
        await author.write("POST", f"/works/{w['id']}/tags", {"name": "多一个"}),
        422,
        "validation_failed",
    )
    removed = await author.write("DELETE", f"/works/{w['id']}/tags/{tag['id']}")
    assert removed.status_code == 200
    assert tag["id"] not in [t["id"] for t in removed.json()["tags"]]

    # 换资源类型后不适用的 tag 被去掉；角色卡必选受众
    card = await _draft(author, "角色卡")
    await _add(author, card["id"], {"tagId": await tag_id("rating", "全年龄")})
    await _add(author, card["id"], {"tagId": await tag_id("type", "角色卡")})
    with_aud = await _add(
        author, card["id"], {"tagId": await tag_id("audience", "男性向")}
    )
    assert any(t["group"] == "audience" for t in with_aud["tags"])
    swapped = await _add(author, card["id"], {"tagId": await tag_id("type", "美化")})
    assert not any(t["group"] == "audience" for t in swapped["tags"])

    # 必答问题：预设回答「含」破限
    preset = await _draft(author, "预设")
    await _add(author, preset["id"], {"tagId": await tag_id("rating", "全年龄")})
    await _add(author, preset["id"], {"tagId": await tag_id("type", "预设")})
    assert_error(await _publish(author, preset["id"]), 422, "validation_failed")
    res = await _publish(
        author, preset["id"], [{"question": "jailbreak", "answer": True}]
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["visibility"] == "restricted" and body["rating"] == "general"
    assert body["declarations"][0]["question"] == "jailbreak"
    # 已发布作品去掉限制可见的 tag 要走审核
    jailbreak = next(t for t in body["tags"] if t["restricts"])
    err = assert_error(
        await author.write("DELETE", f"/works/{preset['id']}/tags/{jailbreak['id']}"),
        403,
        "forbidden",
    )
    assert err["details"]["reason"] == "review_required"

    # 别人：官方 tag 403，新人 onboarding_required，自由 tag 可以加、只能删自己加的
    pub = await _published(author, tag_id, "别人加 tag")
    assert_error(
        await other.write(
            "POST", f"/works/{pub['id']}/tags", {"tagId": await tag_id("theme", "古风")}
        ),
        403,
        "forbidden",
    )
    assert_error(
        await newbie.write("POST", f"/works/{pub['id']}/tags", {"name": "新人"}),
        403,
        "onboarding_required",
    )
    added = await _add(other, pub["id"], {"name": "路人"}, status=(201,))
    mine = next(t for t in added["tags"] if t["name"] == "路人")
    type_tag = next(t for t in added["tags"] if t["group"] == "type")
    assert_error(
        await other.write("DELETE", f"/works/{pub['id']}/tags/{type_tag['id']}"),
        403,
        "forbidden",
    )
    res = await other.write("DELETE", f"/works/{pub['id']}/tags/{mine['id']}")
    assert res.status_code == 200
    # 作者不能把已发布作品的必选组删空
    assert_error(
        await author.write("DELETE", f"/works/{pub['id']}/tags/{type_tag['id']}"),
        422,
        "validation_failed",
    )


@pytest.mark.asyncio
async def test_rating_conflict_and_cover(env):
    app, account, tag_id, factory = env
    user_id = await account("author", access="granted")
    author = await _login(app, "author")
    w = await _draft(author)
    await _add(author, w["id"], {"tagId": await tag_id("rating", "全年龄")})
    await _add(author, w["id"], {"tagId": await tag_id("type", "美化")})
    async with factory() as session:
        att = Attachment(
            owner_id=user_id,
            purpose="cover",
            kind="image",
            filename="c.png",
            mime="image/png",
            size=10,
            storage_key="t/c.png",
            state="ready",
            rating="r18",
        )
        session.add(att)
        await session.flush()
        att_id = str(att.id)
        await session.commit()
    res = await author.write("PATCH", f"/works/{w['id']}", {"coverId": att_id})
    assert res.status_code == 200, res.text
    cover = res.json()["cover"]
    # 作者是 general 模式：R18 封面不给地址
    assert cover["url"] is None and cover["placeholder"] is True
    err = assert_error(await _publish(author, w["id"]), 422, "rating_conflict")
    assert err["details"]["workRating"] == "general"
    assert err["details"]["imageRating"] == "r18"
    assert_error(
        await author.write("PATCH", f"/works/{w['id']}", {"coverId": "999999"}),
        422,
        "validation_failed",
    )


@pytest.mark.asyncio
async def test_favorite_without_discord(env):
    app, account, tag_id, _ = env
    await account("author", access="granted")
    await account("fan")
    author = await _login(app, "author")
    fan = await _login(app, "fan")
    work = await _published(author, tag_id, "收藏")
    anon = Client(app)
    await anon.session()
    assert_error(
        await anon.write("POST", f"/works/{work['id']}/favorite"),
        401,
        "unauthenticated",
    )
    assert (await fan.write("POST", f"/works/{work['id']}/favorite")).status_code == 204
    assert (await fan.write("POST", f"/works/{work['id']}/favorite")).status_code == 204
    assert (await fan.get(f"/works/{work['id']}")).json()["viewer"]["favorited"] is True
    assert (
        await fan.write("DELETE", f"/works/{work['id']}/favorite")
    ).status_code == 204
    assert (await fan.get(f"/works/{work['id']}")).json()["viewer"][
        "favorited"
    ] is False


@pytest.mark.asyncio
async def test_pagination_keyset(env):
    app, account, tag_id, _ = env
    await account("author", access="granted")
    author = await _login(app, "author")
    created = [await _published(author, tag_id, f"分页{i}") for i in range(5)]
    anon = Client(app)
    for sort in ("latest", "new", "hot_week"):
        seen, cursor = [], None
        while True:
            params = {"sort": sort, "limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (await anon.get("/works", params=params)).json()
            seen.extend(w["id"] for w in page["items"])
            cursor = page["nextCursor"]
            if not cursor:
                break
        assert sorted(seen) == sorted(w["id"] for w in created), sort
    assert_error(
        await anon.get("/works", params={"sort": "nope"}), 422, "validation_failed"
    )
    assert_error(
        await anon.get("/works", params={"cursor": "!!"}), 422, "validation_failed"
    )
