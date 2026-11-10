"""/api/v1 聊天外层（3.5）、举报（3.10）、审核队列（3.10）。

FakeFluxer + create_all 的测试库；作品、附件、tag 直接写表（作品和上传领域不在这里）。
"""

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from models.attachment import Attachment
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.site_moderation import ModerationQueueItem, Report, UserModerationRecord
from models.site_tag import SiteTag, SiteTagGroup, SiteWorkTag
from models.work import Work
from models.work_user_mute import WorkUserMute
from shared.time_utils import utc_now
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.fluxer.port import UnreadSummary
from site_v1.services.accounts import ensure_account_rows

BASE = "https://site.test"
PASSWORD = "demo1234"
STAFF_PERMS = ["moderation.images", "moderation.reports"]


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

    async def account(username, *, access="none", r18=False, permissions=()):
        user_id = await fake.ensure_account(
            username, PASSWORD, email=f"{username}@example.com", display_name=username.upper()
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
            await session.commit()
        return user_id

    class Env:
        pass

    e = Env()
    e.app, e.account, e.factory, e.fake = app, account, db_session_factory, fake
    yield e
    set_session_factory(None)
    set_fluxer(None)
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
        return res

    async def post(self, path, json=None):
        if self.csrf is None:
            await self.session()
        return await self.http.post(
            "/api/v1" + path, json=json, headers={"X-CSRF-Token": self.csrf}
        )

    async def get(self, path, **kw):
        return await self.http.get("/api/v1" + path, **kw)

    async def login(self, username):
        await self.session()
        res = await self.post("/auth/login", {"username": username, "password": PASSWORD})
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
    assert body["code"] == code
    return body


async def make_work(env, author_id, title="作品", rating="general", **kw):
    flags = {"general": (False, False), "r18": (True, False), "r18g": (True, True)}[rating]
    async with env.factory() as session:
        work = Work(
            title=title,
            author_id=author_id,
            source_platform="fluxer",
            source_thread_id=kw.pop("thread", ""),
            is_nsfw=flags[0],
            is_abyss=flags[1],
            visibility="public" if rating == "general" else "restricted",
            summary=f"{title}的简介",
            **kw,
        )
        session.add(work)
        await session.flush()
        work_id = work.id
        await session.commit()
        return work_id


@pytest_asyncio.fixture
async def people(env):
    ids = {
        "author": await env.account("author", access="granted", r18=True),
        "plain": await env.account("plain"),
        "other": await env.account("other"),
        "r18": await env.account("viewer18", access="granted", r18=True),
        "staff": await env.account("staff", access="granted", r18=True, permissions=STAFF_PERMS),
        "noaccess": await env.account("staffnoaccess", permissions=STAFF_PERMS),
    }
    return ids


# ---------------- 聊天外层 ----------------


@pytest.mark.asyncio
async def test_chat_targets(env, people):
    general = await make_work(env, people["author"], "交流区作品")
    restricted = await make_work(env, people["author"], "R18 作品", "r18")
    anon = await client(env)
    plain = await client(env, "plain")
    r18 = await client(env, "viewer18")

    res = await anon.get(f"/chat/targets/work/{general}")
    assert res.status_code == 200, res.text
    info = res.json()
    # fake 模式下没有原生主帖也能打开；未登录不能发言
    assert info["target"] == {"kind": "work", "id": str(general)}
    assert info["canOpen"] is True and info["title"] == "交流区作品"
    assert info["memberCount"] == 1 and info["canPost"] is False

    # 受限作品：全年龄查看者 404，r18 有效模式 200
    for c in (anon, plain):
        err(await c.get(f"/chat/targets/work/{restricted}"), 404, "not_found")
    assert (await r18.get(f"/chat/targets/work/{restricted}")).status_code == 200

    err(await anon.get("/chat/targets/work/no-such"), 404, "not_found")
    err(await anon.get("/chat/targets/work/999999"), 404, "not_found")
    err(await anon.get(f"/chat/targets/planet/{general}"), 404, "not_found")
    err(await anon.get("/chat/targets/dm/whatever"), 401, "unauthenticated")
    err(await plain.get("/chat/targets/dm/no-such"), 404, "not_found")

    # 私信：成员才能看到，标题是对方的显示名
    env.fake.dm_seed["dm1"] = {"members": [people["plain"], people["other"]]}
    res = await plain.get("/chat/targets/dm/dm1")
    assert res.status_code == 200, res.text
    assert res.json()["title"] == "OTHER" and res.json()["memberCount"] == 2
    err(await r18.get("/chat/targets/dm/dm1"), 404, "not_found")

    # 作品内禁言、全站禁言反映到 canPost / mutedUntil
    assert (await plain.get(f"/chat/targets/work/{general}")).json()["canPost"] is True
    until = utc_now() + timedelta(days=2)
    async with env.factory() as session:
        session.add(
            WorkUserMute(work_id=general, user_id=people["plain"], actor_id=people["author"], reason="刷屏", expires_at=until)
        )
        await session.commit()
    info = (await plain.get(f"/chat/targets/work/{general}")).json()
    assert info["canPost"] is False and info["mutedUntil"].startswith(until.strftime("%Y-%m-%dT%H:%M"))
    async with env.factory() as session:
        session.add(
            UserModerationRecord(user_id=people["other"], kind="mute", until=until, by_user_id=people["staff"])
        )
        await session.commit()
    other = await client(env, "other")
    assert (await other.get(f"/chat/targets/work/{general}")).json()["canPost"] is False
    assert (await other.get("/chat/targets/dm/dm1")).json()["canPost"] is False
    for c in (anon, plain, r18, other):
        await c.close()


@pytest.mark.asyncio
async def test_chat_target_without_native_thread_outside_fake(env, people, monkeypatch):
    """真实环境：原生主帖没建好时 canOpen=false；建好了才问 Fluxer。"""
    pending = await make_work(env, people["author"], "等待建帖")
    ready = await make_work(env, people["author"], "已建帖", thread="777")
    monkeypatch.setenv("FLUXER_FAKE", "0")
    reload_config()
    anon = await client(env)
    info = (await anon.get(f"/chat/targets/work/{pending}")).json()
    assert info["canOpen"] is False and info["memberCount"] == 0
    info = (await anon.get(f"/chat/targets/work/{ready}")).json()
    assert info["canOpen"] is True
    await anon.close()


@pytest.mark.asyncio
async def test_unread_summary(env, people):
    general = await make_work(env, people["author"], "公开", thread="101")
    restricted = await make_work(env, people["author"], "受限", "r18", thread="102")
    anon = await client(env)
    err(await anon.get("/chat/unread-summary"), 401, "unauthenticated")
    plain = await client(env, "plain")
    assert (await plain.get("/chat/unread-summary")).json() == []

    summary = UnreadSummary(
        works=[
            {"thread_id": "101", "unread": 3, "mentions": 1, "last_message_at": "2026-11-01T10:00:00Z"},
            {"thread_id": "102", "unread": 5, "mentions": 0},
            {"thread_id": "999", "unread": 1, "mentions": 0},
        ],
        dms=[{"channel_id": "dm9", "unread": 2, "mentions": 0, "last_message_at": 1700000000000}],
    )
    env.fake.unread_seed[people["plain"]] = summary
    env.fake.unread_seed[people["r18"]] = summary
    items = (await plain.get("/chat/unread-summary")).json()
    assert items == [
        {"target": {"kind": "work", "id": str(general)}, "count": 3, "mentions": 1, "lastMessageAt": "2026-11-01T10:00:00.000Z"},
        {"target": {"kind": "dm", "id": "dm9"}, "count": 2, "mentions": 0, "lastMessageAt": "2023-11-14T22:13:20.000Z"},
    ]
    r18 = await client(env, "viewer18")
    ids = [i["target"]["id"] for i in (await r18.get("/chat/unread-summary")).json()]
    assert str(restricted) in ids
    for c in (anon, plain, r18):
        await c.close()


# ---------------- 举报 ----------------


@pytest.mark.asyncio
async def test_reports(env, people):
    work = await make_work(env, people["author"], "被举报")
    restricted = await make_work(env, people["author"], "受限", "r18")
    anon = await client(env)
    err(await anon.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "rating_mismatch"}), 401, "unauthenticated")

    plain = await client(env, "plain")
    e = err(await plain.post("/reports", {"targetType": "work", "targetId": str(work)}), 422, "validation_failed")
    assert "reason" in e["details"]["fields"]
    e = err(await plain.post("/reports", {"targetType": "planet", "targetId": str(work), "reason": "other", "note": "x"}), 422, "validation_failed")
    assert "targetType" in e["details"]["fields"]
    e = err(await plain.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "other"}), 422, "validation_failed")
    assert "note" in e["details"]["fields"]
    e = err(await plain.post("/reports", {"targetType": "user", "targetId": people["author"], "reason": "missing_warning"}), 422, "validation_failed")
    assert "reason" in e["details"]["fields"]
    err(await plain.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "illegal", "note": "x" * 501}), 422, "validation_failed")
    err(await plain.post("/reports", {"targetType": "user", "targetId": people["plain"], "reason": "spam"}), 422, "validation_failed")

    res = await plain.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "rating_mismatch", "note": " 有露骨描写 "})
    assert res.status_code == 201, res.text
    receipt = res.json()
    assert set(receipt) == {"id", "createdAt"} and receipt["createdAt"].endswith("Z")
    err(await plain.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "illegal"}), 409, "conflict")

    other = await client(env, "other")
    assert (await other.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "missing_warning"})).status_code == 201
    assert (await other.post("/reports", {"targetType": "user", "targetId": people["author"], "reason": "spam"})).status_code == 201
    assert (await other.post("/reports", {"targetType": "message", "targetId": "m_1", "reason": "harassment"})).status_code == 201

    # 看不到的、不存在的、第二期对象：404
    err(await plain.post("/reports", {"targetType": "work", "targetId": str(restricted), "reason": "rating_mismatch"}), 404, "not_found")
    for t in ("collection", "event", "event_entry"):
        err(await plain.post("/reports", {"targetType": t, "targetId": "no-such", "reason": "other", "note": "x"}), 404, "not_found")
    err(await plain.post("/reports", {"targetType": "user", "targetId": "nobody", "reason": "spam"}), 404, "not_found")
    err(await plain.post("/reports", {"targetType": "upload", "targetId": "123456", "reason": "illegal"}), 404, "not_found")
    r18 = await client(env, "viewer18")
    assert (await r18.post("/reports", {"targetType": "work", "targetId": str(restricted), "reason": "rating_mismatch"})).status_code == 201

    async with env.factory() as session:
        items = (await session.execute(select(ModerationQueueItem).where(ModerationQueueItem.target_id == str(work)))).scalars().all()
        assert len(items) == 1 and items[0].subject_user_id == people["author"]
        assert items[0].target_snapshot["title"] == "被举报"
        reports = (await session.execute(select(Report).where(Report.queue_item_id == items[0].id))).scalars().all()
        assert len(reports) == 2 and {r.note for r in reports} == {"有露骨描写", ""}
    for c in (anon, plain, other, r18):
        await c.close()


@pytest.mark.asyncio
async def test_report_tag_and_upload_visibility(env, people):
    work = await make_work(env, people["author"], "带图")
    async with env.factory() as session:
        session.add(SiteTagGroup(id="theme", name="题材", max_select=5))
        free = SiteTag(group_id="free", name="自由", created_by=people["author"])
        jail = SiteTag(group_id="attribute", name="破限", restricts=True, official=True)
        public_img = Attachment(owner_id=people["author"], purpose="gallery", work_id=work, kind="image", filename="a.png", mime="image/png", size=10, storage_key="k1", state="ready", rating="general")
        r18_img = Attachment(owner_id=people["author"], purpose="gallery", work_id=work, kind="image", filename="b.png", mime="image/png", size=10, storage_key="k2", state="ready", rating="r18")
        session.add_all([free, jail, public_img, r18_img])
        await session.flush()
        ids = free.id, jail.id, public_img.id, r18_img.id
        await session.commit()
    plain = await client(env, "plain")
    assert (await plain.post("/reports", {"targetType": "tag", "targetId": str(ids[0]), "reason": "spam"})).status_code == 201
    err(await plain.post("/reports", {"targetType": "tag", "targetId": str(ids[1]), "reason": "spam"}), 404, "not_found")
    assert (await plain.post("/reports", {"targetType": "upload", "targetId": str(ids[2]), "reason": "illegal"})).status_code == 201
    err(await plain.post("/reports", {"targetType": "upload", "targetId": str(ids[3]), "reason": "illegal"}), 404, "not_found")
    await plain.close()


# ---------------- 审核队列 ----------------


@pytest.mark.asyncio
async def test_queue_permissions_and_report_items(env, people):
    work = await make_work(env, people["author"], "被举报")
    plain = await client(env, "plain")
    other = await client(env, "other")
    for c in (plain, other):
        assert (await c.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "rating_mismatch"})).status_code == 201
    assert (await plain.post("/reports", {"targetType": "user", "targetId": people["author"], "reason": "spam"})).status_code == 201

    err(await plain.get("/moderation/queue", params={"type": "image"}), 403, "forbidden")
    err(await plain.get("/moderation/queue", params={"type": "report"}), 403, "forbidden")
    noaccess = await client(env, "staffnoaccess")
    err(await noaccess.get("/moderation/queue", params={"type": "image"}), 403, "forbidden")
    assert (await noaccess.get("/moderation/queue", params={"type": "report"})).status_code == 200

    staff = await client(env, "staff")
    err(await staff.get("/moderation/queue"), 422, "validation_failed")
    err(await staff.get("/moderation/queue", params={"type": "report", "state": "x"}), 422, "validation_failed")
    page = (await staff.get("/moderation/queue", params={"type": "report"})).json()
    assert page["nextCursor"] is None and len(page["items"]) == 2
    first = page["items"][0]
    # 举报人多的排前面
    assert first["target"]["type"] == "work" and first["target"]["id"] == str(work)
    assert first["type"] == "report" and first["state"] == "pending" and first["resolution"] is None
    assert first["reportCount"] == 2 and first["reasons"] == [{"reason": "rating_mismatch", "count": 2}]
    assert first["target"]["author"]["id"] == people["author"]
    assert first["target"]["work"]["title"] == "被举报" and first["target"]["work"]["rating"] == "general"
    assert first["authorStrikes90d"] == 0 and len(first["reports"]) == 2
    assert first["reports"][0]["reporter"]["username"] in ("plain", "other")

    paged = (await staff.get("/moderation/queue", params={"type": "report", "limit": 1})).json()
    assert len(paged["items"]) == 1 and paged["nextCursor"]
    rest = (await staff.get("/moderation/queue", params={"type": "report", "limit": 1, "cursor": paged["nextCursor"]})).json()
    assert rest["items"][0]["target"]["type"] == "user"
    for c in (plain, other, noaccess, staff):
        await c.close()


@pytest.mark.asyncio
async def test_resolve_dismiss_conflict_and_report_again(env, people):
    work = await make_work(env, people["author"], "被举报")
    plain = await client(env, "plain")
    assert (await plain.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "rating_mismatch"})).status_code == 201
    staff = await client(env, "staff")
    item = (await staff.get("/moderation/queue", params={"type": "report"})).json()["items"][0]

    err(await plain.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "dismiss", "reason": "x"}), 403, "forbidden")
    err(await staff.post("/moderation/queue/999999/resolve", {"decision": "dismiss", "reason": "x"}), 404, "not_found")
    err(await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "approve", "reason": "x"}), 422, "validation_failed")
    err(await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "penalize", "reason": "x"}), 422, "validation_failed")
    err(await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "dismiss", "reason": "x", "penalty": {"kind": "warning"}}), 422, "validation_failed")
    err(await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "remove", "reason": ""}), 422, "validation_failed")

    res = await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "dismiss", "reason": "没问题"})
    assert res.status_code == 200, res.text
    done = res.json()
    assert done["state"] == "resolved" and done["resolution"]["decision"] == "dismiss"
    assert done["resolution"]["by"]["username"] == "staff" and done["resolution"]["at"].endswith("Z")
    assert done["resolution"]["penalty"] is None and done["resolution"]["rating"] is None
    err(await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "dismiss", "reason": "again"}), 409, "conflict")

    resolved = (await staff.get("/moderation/queue", params={"type": "report", "state": "resolved"})).json()
    assert [i["id"] for i in resolved["items"]] == [item["id"]]
    assert (await staff.get("/moderation/queue", params={"type": "report"})).json()["items"] == []
    # 处理后可以再举报，进新的队列项
    assert (await plain.post("/reports", {"targetType": "work", "targetId": str(work), "reason": "illegal"})).status_code == 201
    again = (await staff.get("/moderation/queue", params={"type": "report"})).json()["items"]
    assert len(again) == 1 and again[0]["id"] != item["id"]
    await plain.close()
    await staff.close()


@pytest.mark.asyncio
async def test_resolve_adjust_penalize_remove(env, people):
    work = await make_work(env, people["author"], "分级不实")
    victim = await make_work(env, people["author"], "侵权作品")
    async with env.factory() as session:
        session.add_all([
            SiteTagGroup(id="rating", name="分级", required=True),
            SiteTagGroup(id="warning", name="内容提示", max_select=10),
        ])
        general_tag = SiteTag(group_id="rating", name="全年龄", rating="general", official=True)
        r18_tag = SiteTag(group_id="rating", name="R18", rating="r18", restricts=True, official=True)
        ntr = SiteTag(group_id="warning", name="NTR", official=True)
        session.add_all([general_tag, r18_tag, ntr])
        await session.flush()
        session.add(SiteWorkTag(work_id=work, tag_id=general_tag.id, added_by=people["author"]))
        r18_tag_id, ntr_id = r18_tag.id, ntr.id
        await session.commit()
    plain = await client(env, "plain")
    staff = await client(env, "staff")
    for target in (work, victim):
        assert (await plain.post("/reports", {"targetType": "work", "targetId": str(target), "reason": "rating_mismatch"})).status_code == 201
    assert (await plain.post("/reports", {"targetType": "user", "targetId": people["author"], "reason": "harassment"})).status_code == 201
    items = {
        (i["target"]["type"], i["target"]["id"]): i["id"]
        for i in (await staff.get("/moderation/queue", params={"type": "report"})).json()["items"]
    }

    item = items[("work", str(work))]
    err(await staff.post(f"/moderation/queue/{item}/resolve", {"decision": "adjust", "reason": "x"}), 422, "validation_failed")
    err(await staff.post(f"/moderation/queue/{item}/resolve", {"decision": "adjust", "reason": "x", "addTags": ["424242"]}), 422, "validation_failed")
    res = await staff.post(
        f"/moderation/queue/{item}/resolve",
        {"decision": "adjust", "rating": "r18", "addTags": [str(ntr_id)], "reason": "有露骨内容", "penalty": {"kind": "warning"}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["resolution"]["penalty"] == {"kind": "warning"}
    async with env.factory() as session:
        w = await session.get(Work, work)
        assert (w.is_nsfw, w.is_abyss, w.visibility) == (True, False, "restricted")
        assert w.rating_lock["by"] == people["staff"]
        tags = set((await session.execute(select(SiteWorkTag.tag_id).where(SiteWorkTag.work_id == work, SiteWorkTag.removed_at.is_(None)))).scalars())
        assert tags == {r18_tag_id, ntr_id}
        records = (await session.execute(select(UserModerationRecord).where(UserModerationRecord.user_id == people["author"]))).scalars().all()
        assert {(r.kind, r.strike) for r in records} == {("adjustment", True), ("warning", False)}
        assert "R18" in next(r.summary for r in records if r.kind == "adjustment")

    # 用户：不能下架，只能处罚；禁言写记录，聊天目标里反映出来
    uitem = items[("user", people["author"])]
    err(await staff.post(f"/moderation/queue/{uitem}/resolve", {"decision": "remove", "reason": "x"}), 422, "validation_failed")
    err(await staff.post(f"/moderation/queue/{uitem}/resolve", {"decision": "penalize", "reason": "x", "penalty": {"kind": "mute"}}), 422, "validation_failed")
    res = await staff.post(f"/moderation/queue/{uitem}/resolve", {"decision": "penalize", "reason": "骚扰", "penalty": {"kind": "mute", "days": 3}})
    assert res.status_code == 200, res.text
    author = await client(env, "author")
    info = (await author.get(f"/chat/targets/work/{victim}")).json()
    assert info["canPost"] is False and info["mutedUntil"]

    # 下架：status=removed，同时 V2 软删除；之后举报 404、聊天目标 404
    vitem = items[("work", str(victim))]
    res = await staff.post(f"/moderation/queue/{vitem}/resolve", {"decision": "remove", "reason": "侵权"})
    assert res.status_code == 200, res.text
    assert res.json()["target"]["work"]["status"] == "removed"
    async with env.factory() as session:
        w = await session.get(Work, victim)
        assert w.status == "removed" and w.moderation_state == "removed" and w.deleted_at is not None
    err(await author.get(f"/chat/targets/work/{victim}"), 404, "not_found")
    # 作者 90 天违规次数：调整 + 下架
    page = (await staff.get("/moderation/queue", params={"type": "report", "state": "resolved"})).json()
    assert all(i["authorStrikes90d"] == 2 for i in page["items"])
    for c in (plain, staff, author):
        await c.close()


@pytest.mark.asyncio
async def test_image_queue(env, people):
    work = await make_work(env, people["author"], "图库作品", moderation_state="pending_review")
    async with env.factory() as session:
        def image(key, review_state="pending_review"):
            return Attachment(
                owner_id=people["author"], purpose="gallery", work_id=work, kind="image",
                filename=f"{key}.png", mime="image/png", size=100, storage_key=key, state="ready",
                rating="r18", auto_rating="suspect", review_state=review_state,
                model_scores={"general": 0.4, "r18": 0.5, "r18g": 0.1}, width=800, height=600,
            )
        a1, a2, done = image("a1"), image("a2"), image("a3", review_state="reviewed")
        session.add_all([a1, a2, done])
        await session.flush()
        w = await session.get(Work, work)
        w.gallery_attachment_ids = [a1.id, a2.id, done.id]
        a1_id, a2_id = a1.id, a2.id
        await session.commit()

    staff = await client(env, "staff")
    page = (await staff.get("/moderation/queue", params={"type": "image"})).json()
    assert [i["attachment"]["id"] for i in page["items"]] == [str(a1_id), str(a2_id)]
    item = page["items"][0]
    assert item["type"] == "image" and item["source"] == "model" and item["autoRating"] == "suspect"
    assert item["modelScores"] == {"general": 0.4, "r18": 0.5, "r18g": 0.1}
    assert item["usage"] == "gallery" and item["uploader"]["id"] == people["author"]
    assert item["attachment"]["placeholder"] is False and item["attachment"]["width"] == 800
    assert item["work"]["id"] == str(work) and item["work"]["moderationState"] == "pending_review"
    assert item["appealed"] is False and item["appealNote"] is None
    # 再读不重复入队
    assert len((await staff.get("/moderation/queue", params={"type": "image"})).json()["items"]) == 2

    err(await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "set_rating", "reason": "x"}), 422, "validation_failed")
    err(await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "dismiss", "reason": "x"}), 422, "validation_failed")
    res = await staff.post(f"/moderation/queue/{item['id']}/resolve", {"decision": "set_rating", "rating": "r18", "reason": "确实是 R18"})
    assert res.status_code == 200, res.text
    assert res.json()["attachment"]["reviewState"] == "reviewed" and res.json()["attachment"]["rating"] == "r18"
    async with env.factory() as session:
        w = await session.get(Work, work)
        # 图片比作品高：作品随之上调；还有一张待审，pending_review 保留
        assert (w.is_nsfw, w.visibility, w.moderation_state) == (True, "restricted", "pending_review")

    second = page["items"][1]
    res = await staff.post(f"/moderation/queue/{second['id']}/resolve", {"decision": "block", "reason": "违规"})
    assert res.status_code == 200, res.text
    assert res.json()["attachment"]["state"] == "rejected" and res.json()["attachment"]["rejectReason"] == "blocked"
    async with env.factory() as session:
        w = await session.get(Work, work)
        assert a2_id not in w.gallery_attachment_ids and w.moderation_state == "ok"
        kinds = (await session.execute(select(UserModerationRecord.summary).where(UserModerationRecord.user_id == people["author"]))).scalars().all()
        assert "拦截图片" in kinds and any("图片复核" in k for k in kinds)
    assert (await staff.get("/moderation/queue", params={"type": "image"})).json()["items"] == []
    await staff.close()
