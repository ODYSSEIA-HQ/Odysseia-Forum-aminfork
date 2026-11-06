"""/api/v1 活动（3.22）、分享预览（3.20）、娱乐区（/fun）。

FakeFluxer + create_all 的测试库；作品、活动直接写表。
"""

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.site_event import SiteEvent, SiteEventEntry
from models.site_fun import SiteFunBlackjackShoe, SiteFunWallet
from models.work import Work
from models.work_version import WorkVersion
from shared.time_utils import utc_now
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.services.accounts import ensure_account_rows

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

    from site_v1 import install

    app = FastAPI()
    install(app)

    async def account(username, *, access="none", r18=False, permissions=(), newcomer=False):
        user_id = await fake.ensure_account(
            username, PASSWORD, email=f"{username}@example.com", display_name=username.upper()
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
            if newcomer:
                acc.newcomer_until = utc_now() + timedelta(days=3)
            prefs = await session.get(SiteUserPrefs, user_id)
            prefs.content_mode = "r18" if r18 else "general"
            await session.commit()
        return user_id

    class Env:
        pass

    e = Env()
    e.app, e.account, e.factory = app, account, db_session_factory
    yield e
    set_session_factory(None)
    set_fluxer(None)
    for var in ("FLUXER_FAKE", "SITE_REGISTRATION_MODE"):
        monkeypatch.delenv(var, raising=False)
    reload_config()


class Client:
    def __init__(self, app):
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
        self.csrf = None

    async def session(self):
        res = await self.http.get("/api/v1/session")
        self.csrf = res.json()["csrfToken"]
        return res

    async def send(self, method, path, json=None):
        if self.csrf is None:
            await self.session()
        return await self.http.request(
            method, "/api/v1" + path, json=json, headers={"X-CSRF-Token": self.csrf}
        )

    async def post(self, path, json=None):
        return await self.send("POST", path, json)

    async def patch(self, path, json=None):
        return await self.send("PATCH", path, json)

    async def put(self, path, json=None):
        return await self.send("PUT", path, json)

    async def delete(self, path):
        return await self.send("DELETE", path)

    async def get(self, path, **kw):
        return await self.http.get("/api/v1" + path, **kw)

    async def login(self, username):
        await self.session()
        res = await self.post("/auth/login", {"username": username, "password": PASSWORD})
        assert res.status_code == 200, res.text
        await self.session()
        return self


async def client(env, username=None):
    c = Client(env.app)
    if username:
        await c.login(username)
    return c


def err(res, status, code):
    assert res.status_code == status, res.text
    body = res.json()["error"]
    assert body["code"] == code
    return body


async def make_work(env, author_id, title="作品", rating="general"):
    flags = {"general": (False, False), "r18": (True, False), "r18g": (True, True)}[rating]
    async with env.factory() as session:
        work = Work(
            title=title, author_id=author_id, source_platform="fluxer", source_thread_id="",
            is_nsfw=flags[0], is_abyss=flags[1], status="published",
            visibility="public" if rating == "general" else "restricted", summary=f"{title}的简介",
            published_at=utc_now() - timedelta(days=1),
        )
        session.add(work)
        await session.flush()
        work_id = work.id
        await session.commit()
        return work_id


async def make_event(env, *, slug, organizers=(), audience="all", ratings=("general",), start=-1, end=5,
                     manual="published", max_entries=2, allow_newcomers=True):
    now = utc_now()
    divisions = []
    if "general" in ratings:
        divisions.append({"id": f"{slug}_g", "name": "全年龄组", "ratings": ["general"]})
    if any(r != "general" for r in ratings):
        divisions.append({"id": f"{slug}_r", "name": "R18 组", "ratings": [r for r in ratings if r != "general"]})
    awards = [
        {"id": f"{d['id']}_a1", "divisionId": d["id"], "name": "一等奖", "description": "", "order": 1, "decorRoleId": None}
        for d in divisions
    ]
    async with env.factory() as session:
        e = SiteEvent(
            slug=slug, title=f"活动{slug}", summary="简介", audience=audience, manual_state=manual,
            submit_start_at=now + timedelta(days=start), submit_end_at=now + timedelta(days=end),
            eligibility={"resourceTypes": [], "requiredTags": [], "ratings": list(ratings), "newWorksSince": None,
                         "maxEntriesPerUser": max_entries, "allowNewcomers": allow_newcomers},
            divisions=divisions, awards=awards, organizer_ids=list(organizers), results_draft=[],
        )
        session.add(e)
        await session.flush()
        event_id = e.id
        await session.commit()
        return event_id


@pytest_asyncio.fixture
async def people(env):
    return {
        "staff": await env.account("staff", access="granted", r18=True, permissions=["events.manage"]),
        "author": await env.account("author", access="granted", r18=True),
        "plain": await env.account("plain"),
        "org": await env.account("org", access="granted", r18=True),
        "newbie": await env.account("newbie", newcomer=True),
    }


# ---------------- 活动：可见性 ----------------


@pytest.mark.asyncio
async def test_event_visibility_and_r18_isolation(env, people):
    mixed = await make_event(env, slug="mixed", ratings=("general", "r18"), organizers=[people["org"]])
    night = await make_event(env, slug="night", audience="r18", ratings=("r18",))
    draft = await make_event(env, slug="draft", manual="draft", organizers=[people["org"]])
    g = await make_work(env, people["author"], "全年龄作品")
    r = await make_work(env, people["author"], "R18 作品", "r18")
    async with env.factory() as session:
        session.add(SiteEventEntry(event_id=mixed, division_id="mixed_g", work_id=g, submitted_by=people["author"]))
        session.add(SiteEventEntry(event_id=mixed, division_id="mixed_r", work_id=r, submitted_by=people["author"]))
        await session.commit()

    anon, plain, author, org = (
        await client(env), await client(env, "plain"), await client(env, "author"), await client(env, "org")
    )
    ids = [e["id"] for e in (await anon.get("/events")).json()["items"]]
    assert ids == [str(mixed)]
    # 全年龄：看不到 R18 组、R18 条目不计数
    ev = (await anon.get(f"/events/{mixed}")).json()
    assert [d["id"] for d in ev["divisions"]] == ["mixed_g"]
    assert [a["divisionId"] for a in ev["awards"]] == ["mixed_g"]
    assert ev["entryCount"] == 1 and ev["state"] == "open" and ev["judging"] == "organizer"
    assert ev["viewer"]["cannotSubmitReason"] == "login_required"
    err(await anon.get(f"/events/{mixed}/entries?division=mixed_r"), 404, "not_found")
    # 也能用 slug 打开
    assert (await anon.get("/events/mixed")).json()["id"] == str(mixed)
    # R18 模式
    ev = (await author.get(f"/events/{mixed}")).json()
    assert len(ev["divisions"]) == 2 and ev["entryCount"] == 2
    assert len((await author.get(f"/events/{mixed}/entries")).json()["items"]) == 2
    # audience=r18：全年龄 404，R18 模式能看到
    err(await plain.get(f"/events/{night}"), 404, "not_found")
    assert (await author.get(f"/events/{night}")).status_code == 200
    # 草稿：只有主办人和站务
    err(await plain.get(f"/events/{draft}"), 404, "not_found")
    assert (await org.get(f"/events/{draft}")).json()["viewer"]["canManage"] is True
    staff = await client(env, "staff")
    assert str(draft) in [e["id"] for e in (await staff.get("/events?state=draft")).json()["items"]]
    # 分享预览按未登录算
    assert (await author.get(f"/share/events/{night}")).status_code == 404
    share = (await author.get(f"/share/events/{mixed}")).json()
    assert "1 件参赛作品" in share["description"] and share["title"].startswith("活动：")
    assert (await anon.get(f"/share/works/{g}")).json()["title"] == "全年龄作品"
    err(await author.get(f"/share/works/{r}"), 404, "not_found")
    user_card = (await anon.get(f"/share/users/{people['author']}")).json()
    assert user_card["description"].startswith("1 个作品")


# ---------------- 活动：参赛 ----------------


@pytest.mark.asyncio
async def test_submit_withdraw_and_rules(env, people):
    ev = await make_event(env, slug="open", organizers=[people["org"]], max_entries=1, allow_newcomers=False)
    upcoming = await make_event(env, slug="later", start=2, end=5)
    closed = await make_event(env, slug="past", start=-5, end=-1)
    w1 = await make_work(env, people["author"], "一号")
    w2 = await make_work(env, people["author"], "二号")
    r18 = await make_work(env, people["author"], "R18", "r18")
    other = await make_work(env, people["plain"], "别人的")
    newbie_work = await make_work(env, people["newbie"], "新人作品")
    org_work = await make_work(env, people["org"], "主办人作品")

    author, org, newbie = await client(env, "author"), await client(env, "org"), await client(env, "newbie")
    err(await author.post(f"/events/{upcoming}/entries", {"workId": str(w1)}), 422, "validation_failed")
    body = err(await author.post(f"/events/{closed}/entries", {"workId": str(w1)}), 422, "validation_failed")
    assert body["details"]["reason"] == "deadline_passed"
    err(await author.post(f"/events/{ev}/entries", {"workId": str(other)}), 403, "forbidden")
    body = err(await author.post(f"/events/{ev}/entries", {"workId": str(r18)}), 422, "validation_failed")
    assert body["details"]["reason"] == "not_eligible"
    err(await org.post(f"/events/{ev}/entries", {"workId": str(org_work)}), 403, "forbidden")
    err(await newbie.post(f"/events/{ev}/entries", {"workId": str(newbie_work)}), 403, "onboarding_required")

    res = await author.post(f"/events/{ev}/entries", {"workId": str(w1), "note": "参赛\x07说明"})
    assert res.status_code == 201, res.text
    entry = res.json()
    assert entry["state"] == "accepted" and entry["note"] == "参赛说明" and entry["work"]["id"] == str(w1)
    body = err(await author.post(f"/events/{ev}/entries", {"workId": str(w2)}), 422, "validation_failed")
    assert body["details"]["reason"] == "limit_reached"
    me = (await author.get(f"/events/{ev}")).json()["viewer"]
    assert me["myEntryIds"] == [entry["id"]] and me["cannotSubmitReason"] == "limit_reached"

    res = await author.patch(f"/events/{ev}/entries/{entry['id']}", {"note": "改了"})
    assert res.json()["note"] == "改了"
    err(await org.patch(f"/events/{ev}/entries/{entry['id']}", {"note": "x"}), 404, "not_found")
    assert (await author.delete(f"/events/{ev}/entries/{entry['id']}")).status_code == 204
    # 撤回后可以再交（同一个作品也行）
    res = await author.post(f"/events/{ev}/entries", {"workId": str(w1)})
    assert res.status_code == 201, res.text
    body = err(await author.post(f"/events/{ev}/entries", {"workId": str(w1)}), 422, "validation_failed")
    assert body["details"]["reason"] in ("limit_reached", "already_submitted")

    mine = (await author.get("/me/event-entries")).json()["items"]
    assert len(mine) == 2 and mine[0]["event"]["id"] == str(ev)

    # 关注
    assert (await author.post(f"/events/{ev}/follow")).status_code == 204
    assert (await author.get(f"/events/{ev}")).json()["viewer"]["following"] is True
    assert (await author.delete(f"/events/{ev}/follow")).status_code == 204
    assert (await author.get(f"/events/{ev}")).json()["viewer"]["following"] is False


@pytest.mark.asyncio
async def test_manage_review_results_and_version_lock(env, people):
    ev = await make_event(env, slug="judge", organizers=[people["org"]], start=-5, end=5)
    w = await make_work(env, people["author"], "参赛作品")
    async with env.factory() as session:
        session.add(WorkVersion(work_id=w, name="1.0", created_at=utc_now() - timedelta(days=1)))
        await session.commit()
    author, org, plain, staff = (
        await client(env, "author"), await client(env, "org"), await client(env, "plain"), await client(env, "staff")
    )
    entry = (await author.post(f"/events/{ev}/entries", {"workId": str(w)})).json()
    assert entry["versionId"] is None

    err(await plain.get(f"/events/{ev}/entries/manage"), 403, "forbidden")
    assert len((await org.get(f"/events/{ev}/entries/manage")).json()["items"]) == 1
    err(await org.post(f"/events/{ev}/entries/{entry['id']}/review", {"decision": "reject"}), 422, "validation_failed")
    res = await org.post(f"/events/{ev}/entries/{entry['id']}/review", {"decision": "reject", "reason": "跑题"})
    assert res.json()["state"] == "rejected" and res.json()["stateReason"] == "跑题"
    assert (await plain.get(f"/events/{ev}")).json()["entryCount"] == 0
    assert (await org.get(f"/events/{ev}/entries/manage?state=rejected")).json()["items"][0]["id"] == entry["id"]
    await org.post(f"/events/{ev}/entries/{entry['id']}/review", {"decision": "restore"})

    # 主办人改介绍；改时间要站务
    assert (await org.patch(f"/events/{ev}", {"summary": "新简介"})).json()["summary"] == "新简介"
    err(await org.patch(f"/admin/events/{ev}", {"title": "x"}), 403, "forbidden")

    award = "judge_g_a1"
    err(await org.put(f"/events/{ev}/results", {"awards": [{"awardId": "nope", "entryIds": []}]}), 422, "validation_failed")
    draft = {"awards": [{"awardId": award, "entryIds": [entry["id"]], "notes": {entry["id"]: "好"}}]}
    assert (await org.put(f"/events/{ev}/results", draft)).status_code == 200
    assert (await org.get(f"/events/{ev}/results/draft")).json()["awards"][0]["entryIds"] == [entry["id"]]
    err(await plain.get(f"/events/{ev}/results"), 404, "not_found")
    # 还没截止不能公布；主办人不能公布
    err(await org.post(f"/events/{ev}/results/publish"), 403, "forbidden")
    body = err(await staff.post(f"/events/{ev}/results/publish"), 422, "validation_failed")
    assert body["details"]["reason"] == "not_open"

    # 截止：锁定截止那一刻的最新版本（截止后发的不算）
    async with env.factory() as session:
        e = await session.get(SiteEvent, ev)
        e.submit_end_at = utc_now() - timedelta(minutes=1)
        session.add(WorkVersion(work_id=w, name="2.0", created_at=utc_now()))
        await session.commit()
    detail = (await plain.get(f"/events/{ev}")).json()
    assert detail["state"] == "judging"
    entries = (await plain.get(f"/events/{ev}/entries")).json()["items"]
    assert entries[0]["versionName"] == "1.0" and entries[0]["award"] is None
    err(await author.delete(f"/events/{ev}/entries/{entry['id']}"), 422, "validation_failed")

    res = await staff.post(f"/events/{ev}/results/publish")
    assert res.status_code == 200 and res.json()["state"] == "closed", res.text
    results = (await plain.get(f"/events/{ev}/results")).json()
    assert results["divisions"][0]["awards"][0]["winners"][0]["note"] == "好"
    assert (await plain.get(f"/events/{ev}/entries?sort=award")).json()["items"][0]["award"]["id"] == award
    logs = (await org.get(f"/events/{ev}/log")).json()["items"]
    assert logs[0]["action"] == "results_publish" and {log["action"] for log in logs} >= {"submit", "review", "results"}


@pytest.mark.asyncio
async def test_admin_create_validate_and_organizers(env, people):
    staff, plain = await client(env, "staff"), await client(env, "plain")
    err(await plain.post("/admin/events", {"title": "x"}), 403, "forbidden")
    err(await staff.post("/admin/events", {}), 422, "validation_failed")
    # 只收 R18 的活动必须 audience=r18
    body = err(
        await staff.post("/admin/events", {"title": "R18", "eligibility": {"ratings": ["r18"]}}), 422, "validation_failed"
    )
    assert "audience" in body["details"]["fields"]
    # 混合分级不能放一个组
    body = err(
        await staff.post(
            "/admin/events",
            {"title": "混", "eligibility": {"ratings": ["general", "r18"]},
             "divisions": [{"id": "d", "name": "全部", "ratings": ["general", "r18"]}]},
        ),
        422, "validation_failed",
    )
    assert "divisions" in body["details"]["fields"]
    res = await staff.post(
        "/admin/events",
        {"title": "混合活动", "slug": "mix-1", "eligibility": {"ratings": ["general", "r18"], "maxEntriesPerUser": 99},
         "timeline": {"submitStartAt": "2026-01-01T00:00:00Z", "submitEndAt": "2099-01-01T00:00:00.000Z"}},
    )
    assert res.status_code == 201, res.text
    ev = res.json()
    assert ev["state"] == "draft" and ev["slug"] == "mix-1" and ev["eligibility"]["maxEntriesPerUser"] == 20
    assert len(ev["divisions"]) == 2 or ev["divisions"][0]["ratings"] == ["general"]  # 全年龄模式只看到全年龄组
    err(await plain.get(f"/events/{ev['id']}"), 404, "not_found")
    # 收 R18 的活动：主办人要有 R18 资格
    body = err(await staff.put(f"/admin/events/{ev['id']}/organizers", {"userIds": [people["plain"]]}), 422, "validation_failed")
    res = await staff.put(f"/admin/events/{ev['id']}/organizers", {"userIds": [people["org"]]})
    assert res.json()["organizers"][0]["id"] == people["org"]
    # 0.5.0：返回整个 Event
    assert res.json()["id"] == ev["id"] and res.json()["slug"] == "mix-1" and "eligibility" in res.json()
    assert (await staff.post(f"/admin/events/{ev['id']}/publish")).json()["state"] == "open"
    err(await staff.post(f"/admin/events/{ev['id']}/publish"), 409, "conflict")
    assert (await staff.post(f"/admin/events/{ev['id']}/cancel", {"reason": "测试"})).json()["state"] == "cancelled"
    res = await staff.patch(f"/admin/events/{ev['id']}", {"timeline": {"submitStartAt": "2026-02-01T00:00:00Z", "submitEndAt": "2026-01-01T00:00:00Z"}})
    err(res, 422, "validation_failed")


# ---------------- 娱乐区 ----------------


@pytest.mark.asyncio
async def test_fun_wallet_and_blackjack(env, people):
    from site_v1.services.fun import blackjack as bj

    anon, author = await client(env), await client(env, "author")
    err(await anon.get("/fun/wallet"), 401, "unauthenticated")
    w = (await author.get("/fun/wallet")).json()
    assert w == {"balance": 1000, "lastDailyAt": None, "canClaimDaily": True, "dailyAmount": 200}
    assert (await author.post("/fun/wallet/daily")).json()["wallet"]["balance"] == 1200
    body = err(await author.post("/fun/wallet/daily"), 409, "conflict")
    assert body["details"]["reason"] == "already_claimed"
    board = (await author.get("/fun/leaderboard")).json()
    assert board[0]["rank"] == 1 and board[0]["balance"] == 1200

    err(await author.post("/fun/blackjack/rounds", {"bet": 5}), 422, "validation_failed")
    err(await author.post("/fun/blackjack/rounds", {"bet": 99999}), 422, "validation_failed")
    err(await author.post("/fun/blackjack/rounds", {"bet": "10"}), 422, "validation_failed")

    def c(rank):
        return {"rank": rank, "suit": "spades"}

    # 牌靴固定：玩家 10、6，庄家 9、7（暗牌）；要牌 5 → 21 自动停牌，庄家 16 再要 K 爆
    shoe = [c("10"), c("9"), c("6"), c("7"), c("5"), c("K")] + bj.create_shoe(1)
    async with env.factory() as session:
        session.add(SiteFunBlackjackShoe(user_id=people["author"], cards=shoe, size=len(shoe)))
        await session.commit()
    r = (await author.post("/fun/blackjack/rounds", {"bet": 100})).json()
    assert r["state"] == "playing" and r["balance"] == 1100
    assert r["dealer"]["cards"] == [c("9")] and r["dealer"]["hidden"] == 1 and r["dealer"]["total"] == 9
    assert r["actions"] == ["hit", "stand", "double"]
    body = err(await author.post("/fun/blackjack/rounds", {"bet": 10}), 409, "conflict")
    assert body["details"]["reason"] == "round_in_progress"
    assert (await author.get("/fun/blackjack/rounds/current")).json()["round"]["id"] == r["id"]
    err(await author.post(f"/fun/blackjack/rounds/{r['id']}/actions", {"action": "split"}), 422, "validation_failed")
    r = (await author.post(f"/fun/blackjack/rounds/{r['id']}/actions", {"action": "hit"})).json()
    assert r["state"] == "settled" and r["outcome"] == "dealer_bust" and r["payout"] == 200 and r["net"] == 100
    assert r["balance"] == 1300 and r["dealer"]["hidden"] == 0
    body = err(await author.post(f"/fun/blackjack/rounds/{r['id']}/actions", {"action": "hit"}), 409, "conflict")
    assert body["details"]["reason"] == "round_settled"
    other = await client(env, "plain")
    err(await other.post(f"/fun/blackjack/rounds/{r['id']}/actions", {"action": "hit"}), 404, "not_found")
    async with env.factory() as session:
        assert (await session.get(SiteFunWallet, people["author"])).balance == 1300


def test_blackjack_rules():
    from site_v1.services.fun import blackjack as bj

    def c(rank):
        return {"rank": rank, "suit": "hearts"}

    assert bj.hand_value([c("A"), c("K")]) == (21, True)
    assert bj.hand_value([c("A"), c("A"), c("9")]) == (21, True)
    assert bj.hand_value([c("A"), c("9"), c("5")]) == (15, False)
    assert bj.decide_outcome([c("A"), c("K")], [c("9"), c("9")]) == "blackjack"
    assert bj.decide_outcome([c("A"), c("K")], [c("A"), c("Q")]) == "push"
    assert bj.decide_outcome([c("K"), c("Q"), c("5")], [c("9")]) == "bust"
    assert bj.payout_for("blackjack", 15) == 15 + 22
    assert len(bj.create_shoe()) == 312


@pytest.mark.asyncio
async def test_stocks_market_and_orders(env, people, monkeypatch):
    from site_v1.services.fun import stocks

    monkeypatch.setattr(stocks, "HISTORY_DAYS", 2)
    anon, author = await client(env), await client(env, "author")
    assert len((await anon.get("/fun/stocks/companies")).json()) == 12
    snap = (await anon.get("/fun/stocks/market")).json()
    assert len(snap["quotes"]) == 12 and snap["rules"]["maxOrderValue"] == 2000
    daily = (await anon.get("/fun/stocks/OAI/history?range=daily")).json()
    assert daily["bars"] and daily["bars"][-1]["label"]
    assert (await anon.get("/fun/stocks/index/history")).json()["intraday"]
    err(await anon.get("/fun/stocks/NOPE/history"), 404, "not_found")
    err(await anon.get("/fun/stocks/OAI/history?range=week"), 422, "validation_failed")
    assert isinstance((await anon.get("/fun/stocks/news?symbol=ANT&limit=5")).json(), list)
    err(await anon.post("/fun/stocks/orders/preview", {"symbol": "OAI", "side": "buy", "qty": 1}), 401, "unauthenticated")

    price = next(q["price"] for q in (await author.get("/fun/stocks/market")).json()["quotes"] if q["symbol"] == "MMX")
    p = (await author.post("/fun/stocks/orders/preview", {"symbol": "mmx", "side": "buy", "qty": 5})).json()
    assert p["symbol"] == "MMX" and p["blocked"] is None and p["total"] >= p["gross"]
    big = (await author.post("/fun/stocks/orders/preview", {"symbol": "MMX", "side": "buy", "qty": 1000})).json()
    assert big["blocked"]["reason"] == "order_limit"
    sell = (await author.post("/fun/stocks/orders/preview", {"symbol": "MMX", "side": "sell", "qty": 1})).json()
    assert sell["blocked"]["reason"] == "shares"
    err(await author.post("/fun/stocks/orders/preview", {"symbol": "MMX", "side": "buy", "qty": 0}), 422, "validation_failed")

    order = {"symbol": "MMX", "side": "buy", "qty": 5, "clientOrderId": "o-1", "refPrice": price, "maxSlipBp": 500}
    res = await author.post("/fun/stocks/orders", order)
    assert res.status_code == 200, res.text
    first = res.json()
    assert first["account"]["positions"][0]["qty"] == 5
    assert first["account"]["balance"] == 1000 - first["trade"]["total"]
    # 幂等：同一个订单号只成交一次
    again = (await author.post("/fun/stocks/orders", order)).json()
    assert again["trade"]["id"] == first["trade"]["id"] and again["account"]["positions"][0]["qty"] == 5
    body = err(
        await author.post("/fun/stocks/orders", {**order, "clientOrderId": "o-2", "refPrice": price * 2, "maxSlipBp": 10}),
        409, "conflict",
    )
    assert body["details"]["reason"] == "slippage"
    body = err(
        await author.post("/fun/stocks/orders", {**order, "side": "sell", "qty": 50, "clientOrderId": "o-3"}),
        422, "validation_failed",
    )
    assert body["details"]["reason"] == "shares"
    now_price = next(q["price"] for q in (await author.get("/fun/stocks/market")).json()["quotes"] if q["symbol"] == "MMX")
    res = await author.post(
        "/fun/stocks/orders", {**order, "side": "sell", "qty": 5, "clientOrderId": "o-4", "refPrice": now_price}
    )
    assert res.status_code == 200, res.text
    me = (await author.get("/fun/stocks/me")).json()
    assert me["positions"] == [] and len(me["trades"]) == 2 and me["trades"][0]["side"] == "sell"
    board = (await author.get("/fun/stocks/leaderboard")).json()
    assert board[0]["user"]["id"] == people["author"]
