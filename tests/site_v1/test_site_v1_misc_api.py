# ruff: noqa: F811  （从别的测试模块导入的 fixture 被当成参数名重定义）
"""wt3/misc 这一批：活动资格 tag 的写法、Work.events 角标、获奖发装饰身份组、
POST /admin/tags 两种响应形状、举报活动/参赛作品/书单、活动推广和搜索联想里的活动。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from models.site_event import SiteEvent, SiteEventEntry, SiteEventLog
from models.site_me import SiteDecorationRole, SiteUserDecorationGrant
from models.site_tag import SiteTag, SiteWorkTag
from shared.time_utils import utc_now
from test_site_v1_events_api import (  # noqa: F401
    client,
    env,
    err,
    make_event,
    make_work,
    people,
)


async def make_tags(env):
    async with env.factory() as session:
        card = SiteTag(group_id="type", name="角色卡", official=True)
        world = SiteTag(group_id="type", name="世界书", official=True)
        gufeng = SiteTag(group_id="theme", name="古风", official=True)
        session.add_all([card, world, gufeng])
        await session.flush()
        ids = {"card": card.id, "world": world.id, "gufeng": gufeng.id}
        await session.commit()
    return ids


async def tag_work(env, work_id, tag_id):
    async with env.factory() as session:
        session.add(SiteWorkTag(work_id=work_id, tag_id=tag_id))
        await session.commit()


# ---------------- 活动资格里的 tag：对外写成「组:名字」 ----------------


@pytest.mark.asyncio
async def test_eligibility_tags_use_group_name_labels(env, people):
    tags = await make_tags(env)
    staff = await client(env, "staff")
    res = await staff.post(
        "/admin/events",
        {
            "title": "角色卡活动",
            # 数字 ID（后端 tag 目录的写法）和「组:名字」（前端假后端的写法）都认
            "eligibility": {
                "ratings": ["general"],
                "resourceTypes": [str(tags["card"]), "type:世界书"],
                "requiredTags": ["theme:古风"],
            },
            "timeline": {"submitStartAt": "2026-01-01T00:00:00Z", "submitEndAt": "2099-01-01T00:00:00Z"},
        },
    )
    assert res.status_code == 201, res.text
    ev = res.json()
    assert ev["eligibility"]["resourceTypes"] == ["type:角色卡", "type:世界书"]
    assert ev["eligibility"]["requiredTags"] == ["theme:古风"]
    # 库里统一存数字 ID
    async with env.factory() as session:
        row = await session.get(SiteEvent, int(ev["id"]))
        assert row.eligibility["resourceTypes"] == [str(tags["card"]), str(tags["world"])]
        assert row.eligibility["requiredTags"] == [str(tags["gufeng"])]
    # 前端编辑时把读回来的「组:名字」原样提交：不变
    res = await staff.patch(f"/admin/events/{ev['id']}", {"eligibility": ev["eligibility"]})
    assert res.json()["eligibility"]["resourceTypes"] == ["type:角色卡", "type:世界书"]
    # 不存在的 tag：422
    body = err(
        await staff.patch(
            f"/admin/events/{ev['id']}",
            {"eligibility": {**ev["eligibility"], "resourceTypes": ["type:不存在"]}},
        ),
        422,
        "validation_failed",
    )
    assert "eligibility" in body["details"]["fields"]
    assert (await staff.post(f"/admin/events/{ev['id']}/publish")).status_code == 200

    # 参赛资格按 tag 判断
    author = await client(env, "author")
    w = await make_work(env, people["author"], "古风角色卡")
    body = err(await author.post(f"/events/{ev['id']}/entries", {"workId": str(w)}), 422, "validation_failed")
    assert body["details"]["reason"] == "not_eligible"
    await tag_work(env, w, tags["card"])
    body = err(await author.post(f"/events/{ev['id']}/entries", {"workId": str(w)}), 422, "validation_failed")
    assert body["message"] == "缺少必带 tag"
    await tag_work(env, w, tags["gufeng"])
    assert (await author.post(f"/events/{ev['id']}/entries", {"workId": str(w)})).status_code == 201
    # 列表里也是同样的写法
    listed = (await author.get("/events")).json()["items"]
    assert listed[0]["eligibility"]["resourceTypes"] == ["type:角色卡", "type:世界书"]


# ---------------- Work.events 角标 ----------------


@pytest.mark.asyncio
async def test_work_events_badges(env, people):
    shown = await make_event(env, slug="shown", start=-5, end=-1)
    night = await make_event(env, slug="night", audience="r18", ratings=("r18",))
    cancelled = await make_event(env, slug="gone", manual="cancelled")
    rejected = await make_event(env, slug="nope")
    w = await make_work(env, people["author"], "参赛作品")
    async with env.factory() as session:
        entry = SiteEventEntry(event_id=shown, division_id="shown_g", work_id=w, submitted_by=people["author"])
        session.add(entry)
        session.add(SiteEventEntry(event_id=cancelled, division_id="gone_g", work_id=w, submitted_by=people["author"]))
        session.add(
            SiteEventEntry(event_id=rejected, division_id="nope_g", work_id=w, submitted_by=people["author"], state="rejected")
        )
        # R18 活动里的条目（作品本身是全年龄的，只看活动可见性）
        session.add(SiteEventEntry(event_id=night, division_id="night_r", work_id=w, submitted_by=people["author"]))
        await session.flush()
        entry_id = entry.id
        await session.commit()

    plain, author = await client(env, "plain"), await client(env, "author")
    refs = (await plain.get(f"/works/{w}")).json()["events"]
    assert refs == [{"eventId": str(shown), "title": "活动shown", "entryId": str(entry_id), "award": None}]
    # R18 模式能看到 R18 活动
    assert {r["eventId"] for r in (await author.get(f"/works/{w}")).json()["events"]} == {str(shown), str(night)}
    # 列表里的作品卡也带
    items = (await plain.get(f"/users/{people['author']}/works")).json()["items"]
    assert items[0]["events"][0]["eventId"] == str(shown)

    # 公布结果后带奖项
    async with env.factory() as session:
        row = await session.get(SiteEventEntry, entry_id)
        row.award_id = "shown_g_a1"
        e = await session.get(SiteEvent, shown)
        e.results_published_at = utc_now()
        await session.commit()
    refs = (await plain.get(f"/works/{w}")).json()["events"]
    assert refs[0]["award"] == {"id": "shown_g_a1", "name": "一等奖"}


# ---------------- 获奖发装饰身份组 ----------------


async def award_event(env, people, slug, decor_role):
    ev = await make_event(env, slug=slug, start=-5, end=-1)
    w = await make_work(env, people["author"], f"{slug}作品")
    async with env.factory() as session:
        e = await session.get(SiteEvent, ev)
        e.awards = [{**e.awards[0], "decorRoleId": decor_role}]
        en = SiteEventEntry(event_id=ev, division_id=f"{slug}_g", work_id=w, submitted_by=people["author"])
        session.add(en)
        await session.flush()
        e.results_draft = [{"awardId": f"{slug}_g_a1", "entryIds": [str(en.id)], "notes": {}}]
        await session.commit()
    return ev


@pytest.mark.asyncio
async def test_results_publish_grants_decoration(env, people):
    async with env.factory() as session:
        session.add(SiteDecorationRole(id="deco_gold", name="金奖", color="#ffaa00", rarity="legendary", slots=["frame"]))
        await session.commit()
    granter = await env.account("granter", access="granted", r18=True, permissions=["events.manage", "roles.grant_decor"])
    ev = await award_event(env, people, "gold", "deco_gold")
    staff = await client(env, "granter")
    assert (await staff.post(f"/events/{ev}/results/publish")).status_code == 200
    author = await client(env, "author")
    wall = (await author.get("/me/decorations")).json()
    gold = next(r for r in wall["decorationRoles"] if r["id"] == "deco_gold")
    assert gold["source"] == "活动：活动gold 一等奖" and gold["rarity"] == "legendary"
    # 他人资料页的勋章墙也有（和 /me/decorations 同一份数据）
    profile = (await (await client(env)).get(f"/users/{people['author']}")).json()
    assert [r["id"] for r in profile["decorationRoles"]] == ["deco_gold"]
    assert profile["decorations"]["frame"] == "#ffaa00"
    # 再公布一次不重复发
    assert (await staff.post(f"/events/{ev}/results/publish")).status_code == 200
    async with env.factory() as session:
        grants = (await session.execute(select(SiteUserDecorationGrant))).scalars().all()
        assert len(grants) == 1 and grants[0].user_id == people["author"]
        logs = (await session.execute(select(SiteEventLog).where(SiteEventLog.event_id == ev))).scalars().all()
        assert any("发放勋章给 AUTHOR" in log.detail for log in logs)
    assert granter

    # 身份组还不存在时按「活动·奖项」建一个
    ev2 = await award_event(env, people, "silver", "deco_new")
    assert (await staff.post(f"/events/{ev2}/results/publish")).status_code == 200
    async with env.factory() as session:
        role = await session.get(SiteDecorationRole, "deco_new")
        assert role is not None and role.name == "活动silver·一等奖"


@pytest.mark.asyncio
async def test_results_publish_without_grant_permission(env, people):
    ev = await award_event(env, people, "nogrant", "deco_x")
    staff = await client(env, "staff")  # 只有 events.manage
    assert (await staff.post(f"/events/{ev}/results/publish")).status_code == 200
    async with env.factory() as session:
        assert (await session.execute(select(SiteUserDecorationGrant))).scalars().all() == []
        logs = (await session.execute(select(SiteEventLog).where(SiteEventLog.event_id == ev))).scalars().all()
        assert any("未发勋章" in log.detail for log in logs)


# ---------------- POST /admin/tags、同义词：两种响应形状 ----------------


@pytest.mark.asyncio
async def test_admin_tags_dual_shape(env, people):
    await make_tags(env)
    await env.account("tagger", permissions=["tags.manage"])
    admin = await client(env, "tagger")
    res = await admin.post(
        "/admin/tags",
        {"name": "仙侠", "group": "theme", "description": "简介", "requiredQuestionFor": ["type:角色卡"]},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    # 协议：TagDetail；前端：{tag, affectedWorks}
    assert body["name"] == "仙侠" and body["group"] == "theme" and body["official"] is True
    assert body["tag"]["id"] == body["id"] and body["affectedWorks"] == 0
    assert body["requiredQuestionFor"] == ["角色卡"]
    res = await admin.post(f"/admin/tags/{body['id']}/synonyms", {"names": ["xianxia"]})
    assert res.status_code == 200, res.text
    syn = res.json()
    assert syn["aliases"] == ["xianxia"] and syn["tag"]["aliases"] == ["xianxia"] and syn["affectedWorks"] == 0


# ---------------- 举报：活动、参赛作品、书单 ----------------


@pytest.mark.asyncio
async def test_report_event_targets(env, people):
    ev = await make_event(env, slug="rep")
    night = await make_event(env, slug="rep-night", audience="r18", ratings=("r18",))
    w = await make_work(env, people["author"], "被举报的参赛作品")
    async with env.factory() as session:
        en = SiteEventEntry(event_id=ev, division_id="rep_g", work_id=w, submitted_by=people["author"], note="说明")
        session.add(en)
        await session.flush()
        entry_id = en.id
        await session.commit()
    plain = await client(env, "plain")
    assert (await plain.post("/reports", {"targetType": "event", "targetId": str(ev), "reason": "spam"})).status_code == 201
    assert (await plain.post("/reports", {"targetType": "event", "targetId": "rep", "reason": "illegal"})).status_code in (201, 409)
    res = await plain.post("/reports", {"targetType": "event_entry", "targetId": str(entry_id), "reason": "rating_mismatch"})
    assert res.status_code == 201, res.text
    # 看不到的活动、不存在的对象：404
    err(await plain.post("/reports", {"targetType": "event", "targetId": str(night), "reason": "spam"}), 404, "not_found")
    for t in ("collection", "event", "event_entry"):
        err(await plain.post("/reports", {"targetType": t, "targetId": "no-such", "reason": "other", "note": "x"}), 404, "not_found")


# ---------------- 活动推广、搜索联想 ----------------


@pytest.mark.asyncio
async def test_event_promotion_and_search_suggest(env, people):
    ev = await make_event(env, slug="promo")
    night = await make_event(env, slug="promo-night", audience="r18", ratings=("r18",))
    await env.account("homer", permissions=["home.manage"])
    admin = await client(env, "homer")
    now = utc_now()
    promo = {
        "placement": "grid",
        "weight": 5,
        "startAt": (now - timedelta(hours=1)).isoformat() + "Z",
        "endAt": (now + timedelta(days=3)).isoformat() + "Z",
    }
    body = err(
        await admin.put("/admin/home/featured", {"editorPicks": [], "promotions": [{**promo, "kind": "event", "targetId": "nope"}]}),
        422,
        "validation_failed",
    )
    assert "不存在" in str(body["details"])
    res = await admin.put(
        "/admin/home/featured",
        {"editorPicks": [], "promotions": [
            {**promo, "kind": "event", "targetId": str(ev)},
            {**promo, "kind": "event", "targetId": str(night)},
        ]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["promotions"][0]["event"]["id"] == str(ev)
    plain, author = await client(env, "plain"), await client(env, "author")
    items = (await plain.get("/home/promotions")).json()["items"]
    assert [(i["kind"], i["event"]["id"]) for i in items] == [("event", str(ev))]
    assert {i["event"]["id"] for i in (await author.get("/home/promotions")).json()["items"]} == {str(ev), str(night)}

    sug = (await plain.get("/search/suggest", params={"q": "活动promo"})).json()
    assert sug["events"] == [{"id": str(ev), "title": "活动promo"}]
    assert {e["id"] for e in (await author.get("/search/suggest", params={"q": "promo"})).json()["events"]} == {
        str(ev), str(night)
    }
