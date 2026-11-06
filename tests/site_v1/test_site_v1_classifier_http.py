"""图片分级 http 适配全链路：上传 → complete → HttpClassifier 调本地假分级服务 → 附件、作品、审核队列。

假分级服务 scripts/fake_classifier.py 和站点本身都用 uvicorn 在测试的事件循环里起（端口 8834/8835，
可用 SITE_IT_SITE_PORT / SITE_IT_CLASSIFIER_PORT 改）：分级服务真的按签名地址回来拉图。
存储用本地存储，SITE_PUBLIC_BASE 指向站点端口。
"""

import asyncio
import hashlib
import importlib.util
import os
import socket
import zlib
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI

from models.attachment import Attachment
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.site_moderation import ModerationQueueItem
from models.work import Work
from shared.time_utils import utc_now
from site_v1.classifier import HttpClassifier, get_classifier
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.files_config import get_files_config, reload_files_config
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.services.accounts import ensure_account_rows
from sqlalchemy import select

from test_site_v1_files_api import PNG, PASSWORD, Client

ROOT = Path(__file__).resolve().parents[2]
SITE_PORT = int(os.environ.get("SITE_IT_SITE_PORT", "8834"))
CLS_PORT = int(os.environ.get("SITE_IT_CLASSIFIER_PORT", "8835"))
TOKEN = "cls-secret"
STAFF_PERMS = ["moderation.images", "moderation.reports"]

_spec = importlib.util.spec_from_file_location("fake_classifier", ROOT / "scripts/fake_classifier.py")
fake_classifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_classifier)


def png(text: str) -> bytes:
    """1×1 PNG，带一个 tEXt 块（内容不同 → 哈希不同，免得撞上「同图复核结果复用」）。"""
    data = b"comment\x00" + text.encode()
    chunk = b"tEXt" + data
    block = len(data).to_bytes(4, "big") + chunk + zlib.crc32(chunk).to_bytes(4, "big")
    return PNG[:33] + block + PNG[33:]


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


ENV_VARS = (
    "FLUXER_FAKE",
    "SITE_REGISTRATION_MODE",
    "SITE_STORAGE_BACKEND",
    "SITE_STORAGE_LOCAL_DIR",
    "SITE_STORAGE_SIGNING_KEY",
    "SITE_PUBLIC_BASE",
    "SITE_CLASSIFIER",
    "SITE_CLASSIFIER_URL",
    "SITE_CLASSIFIER_TOKEN",
    "SITE_CLASSIFIER_TIMEOUT",
    "SITE_CLASSIFY_INLINE_WAIT",
)


async def _serve(app, port):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, f"端口 {port} 起不来"
    return server, task


class Env:
    pass


@pytest_asyncio.fixture
async def cenv(monkeypatch, tmp_path, db_session_factory, redis_client):
    if not (_port_free(SITE_PORT) and _port_free(CLS_PORT)):
        pytest.skip(f"端口 {SITE_PORT}/{CLS_PORT} 被占用")
    monkeypatch.setenv("FLUXER_FAKE", "1")
    monkeypatch.setenv("SITE_REGISTRATION_MODE", "open")
    monkeypatch.setenv("SITE_STORAGE_BACKEND", "local")
    monkeypatch.setenv("SITE_STORAGE_LOCAL_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SITE_STORAGE_SIGNING_KEY", "test-key")
    monkeypatch.setenv("SITE_PUBLIC_BASE", f"http://127.0.0.1:{SITE_PORT}")
    monkeypatch.setenv("SITE_CLASSIFIER", "http")
    monkeypatch.setenv("SITE_CLASSIFIER_URL", f"http://127.0.0.1:{CLS_PORT}/classify")
    monkeypatch.setenv("SITE_CLASSIFIER_TOKEN", TOKEN)
    monkeypatch.setenv("SITE_CLASSIFIER_TIMEOUT", "1")
    monkeypatch.setenv("SITE_CLASSIFY_INLINE_WAIT", "5")
    reload_config()
    reload_files_config()
    assert isinstance(get_classifier(), HttpClassifier)
    set_session_factory(db_session_factory)
    fake = FakeFluxer(session_factory=db_session_factory, redis=redis_client)
    set_fluxer(fake)

    from site_v1 import install

    app = FastAPI()
    install(app)
    cls_app = fake_classifier.create_app(token=TOKEN, sleep=2.5)
    site_server, site_task = await _serve(app, SITE_PORT)
    cls_server, cls_task = await _serve(cls_app, CLS_PORT)

    env = Env()
    env.app, env.factory, env.cls = app, db_session_factory, cls_app

    async def account(username, *, r18=False, permissions=()):
        user_id = await fake.ensure_account(username, PASSWORD, email=f"{username}@example.com")
        async with db_session_factory() as session:
            await ensure_account_rows(session, user_id)
            row = await session.get(SiteRestrictedAccess, user_id)
            row.state = "granted"
            row.age_confirmed_at = utc_now()
            row.terms_version = "2026-10"
            acc = await session.get(SiteAccount, user_id)
            acc.permissions = list(permissions)
            if r18:
                prefs = await session.get(SiteUserPrefs, user_id)
                prefs.content_mode = "r18"
            await session.commit()
        return user_id

    async def work(author_id, *, status="published", rating="general"):
        async with db_session_factory() as session:
            w = Work(
                title="分级测试作品",
                author_id=author_id,
                status=status,
                is_nsfw=rating != "general",
                is_abyss=rating == "r18g",
                visibility="public" if rating == "general" else "restricted",
            )
            session.add(w)
            await session.commit()
            await session.refresh(w)
            return w.id

    env.account, env.work = account, work
    yield env

    for server in (site_server, cls_server):
        server.should_exit = True
    await asyncio.gather(site_task, cls_task, return_exceptions=True)
    set_session_factory(None)
    set_fluxer(None)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reload_config()
    reload_files_config()


async def upload(c: Client, wid, data: bytes, filename: str, *, purpose="gallery"):
    up, state = await c.upload(wid, data=data, purpose=purpose, filename=filename)
    att = (await c.get(f"/uploads/{up}")).json()
    return up, state, att


async def patch(c: Client, path: str, body: dict):
    return await c.http.patch("/api/v1" + path, json=body, headers={"X-CSRF-Token": c.csrf})


@pytest.mark.asyncio
async def test_http_classifier_request_shape_and_ratings(cenv):
    uid = await cenv.account("author", r18=True)
    published = await cenv.work(uid)
    draft = await cenv.work(uid, status="draft")
    c = Client(cenv.app)
    await c.login("author")

    # 全年龄：ready；分级服务拿到的请求形状、拉到的字节都对
    data = png("general")
    up, state, att = await upload(c, published, data, "cover.png", purpose="cover")
    assert state == "ready" and att["rating"] == "general" and att["reviewState"] == "auto"
    (req,) = cenv.cls.state.requests
    body = req["body"]
    assert set(body) == {"attachmentId", "url", "mime", "filename", "size", "sha256", "width", "height"}
    assert body["attachmentId"] == up and body["mime"] == "image/png" and body["filename"] == "cover.png"
    assert body["size"] == len(data) and (body["width"], body["height"]) == (1, 1)
    assert body["url"].startswith(f"http://127.0.0.1:{SITE_PORT}/api/storage/files/")
    assert req["error"] is None
    assert req["fetched"]["sha256"] == hashlib.sha256(data).hexdigest() == body["sha256"]
    async with cenv.factory() as session:
        row = await session.get(Attachment, int(up))
        assert row.auto_rating == "general" and row.model_scores["general"] == 0.98
        assert row.sha256 == body["sha256"]

    # 结果进作品：设成封面，作品详情里有封面地址
    res = await patch(c, f"/works/{published}", {"coverId": up})
    assert res.status_code == 200, res.text
    detail = (await c.get(f"/works/{published}")).json()
    assert detail["cover"]["id"] == up and detail["cover"]["url"]

    # 已发布的全年龄作品传 R18：分级冲突拦截
    up, state, att = await upload(c, published, png("r18"), "pic-r18.png")
    assert state == "rejected" and att["rejectReason"] == "rating_conflict" and att["rating"] == "r18"
    # 草稿没定分级：R18G 收下
    up, state, att = await upload(c, draft, png("r18g"), "pic-r18g.png")
    assert state == "ready" and att["rating"] == "r18g"
    # 违禁
    up, state, att = await upload(c, draft, png("blocked"), "blocked.png")
    assert state == "rejected" and att["rejectReason"] == "blocked"
    async with cenv.factory() as session:
        row = await session.get(Attachment, int(up))
        assert row.auto_rating == "blocked"
    await c.close()


@pytest.mark.asyncio
async def test_low_confidence_goes_to_queue_and_review_result_is_reused(cenv):
    uid = await cenv.account("author", r18=True)
    await cenv.account("staff", r18=True, permissions=STAFF_PERMS)
    general = await cenv.work(uid)
    r18 = await cenv.work(uid, rating="r18")
    c = Client(cenv.app)
    await c.login("author")

    data = png("lowconf")
    up, state, att = await upload(c, general, data, "maybe-lowconf.png")
    # 拿不准：全年龄作品先按 R18 放行，待人工；作品标 pending_review
    assert state == "ready" and att["rating"] == "r18" and att["reviewState"] == "pending_review"
    # R18 作品上拿不准：先按 R18G
    up2, state2, att2 = await upload(c, r18, png("lowconf-2"), "lowconf.png")
    assert state2 == "ready" and att2["rating"] == "r18g" and att2["reviewState"] == "pending_review"
    # 服务自己判 suspect
    up3, _, att3 = await upload(c, r18, png("svc"), "svc-suspect.png")
    assert att3["reviewState"] == "pending_review" and att3["rating"] == "r18g"

    async with cenv.factory() as session:
        w = await session.get(Work, general)
        assert w.moderation_state == "pending_review"
        queued = (
            await session.execute(
                select(ModerationQueueItem.attachment_id).where(
                    ModerationQueueItem.type == "image", ModerationQueueItem.state == "pending"
                )
            )
        ).scalars().all()
        assert set(queued) == {int(up), int(up2), int(up3)}  # complete 时直接入队

    staff = Client(cenv.app)
    await staff.login("staff")
    page = (await staff.get("/moderation/queue", params={"type": "image"})).json()
    item = next(i for i in page["items"] if i["attachment"]["id"] == up)
    assert item["autoRating"] == "suspect" and item["source"] == "model"
    assert item["modelScores"] == {"general": 0.55, "r18": 0.4, "r18g": 0.05}
    assert item["work"]["moderationState"] == "pending_review"
    res = await staff.post(
        f"/moderation/queue/{item['id']}/resolve",
        {"decision": "set_rating", "rating": "general", "reason": "其实没问题"},
    )
    assert res.status_code == 200, res.text
    att = (await c.get(f"/uploads/{up}")).json()
    assert att["rating"] == "general" and att["reviewState"] == "reviewed" and att["url"]
    async with cenv.factory() as session:
        assert (await session.get(Work, general)).moderation_state == "ok"

    # 同一张图再传：直接用复核结果，不再调分级服务
    calls = len(cenv.cls.state.requests)
    again, state, att = await upload(c, general, data, "same.png")
    assert state == "ready" and att["rating"] == "general" and att["reviewState"] == "reviewed"
    assert len(cenv.cls.state.requests) == calls
    for x in (c, staff):
        await x.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["error.png", "garbage.png", "nan.png", "timeout.png"])
async def test_service_failures_fall_back_to_manual_review(cenv, name):
    uid = await cenv.account("author")
    wid = await cenv.work(uid)
    c = Client(cenv.app)
    await c.login("author")
    up, state, att = await upload(c, wid, png(name), name)
    # 服务挂了、超时、乱回：不放行成全年龄，按 R18 待人工
    assert state == "ready" and att["rating"] == "r18" and att["reviewState"] == "pending_review"
    async with cenv.factory() as session:
        row = await session.get(Attachment, int(up))
        assert row.auto_rating == "suspect" and row.model_scores is None
    await c.close()


@pytest.mark.asyncio
async def test_wrong_token_and_slow_classification(cenv, monkeypatch):
    uid = await cenv.account("author")
    wid = await cenv.work(uid)
    c = Client(cenv.app)
    await c.login("author")

    # 令牌不对：分级服务 401 → 拿不准
    monkeypatch.setenv("SITE_CLASSIFIER_TOKEN", "wrong")
    reload_files_config()
    _, state, att = await upload(c, wid, png("tok"), "a.png")
    assert att["reviewState"] == "pending_review"

    # complete 只等一小会儿：先返回 processing，分级完成后 GET 拿到结果
    monkeypatch.setenv("SITE_CLASSIFIER_TOKEN", TOKEN)
    monkeypatch.setenv("SITE_CLASSIFIER_TIMEOUT", "5")
    monkeypatch.setenv("SITE_CLASSIFY_INLINE_WAIT", "0.2")
    reload_files_config()
    assert get_files_config().classify_inline_wait == 0.2
    up, state = await c.upload(wid, data=png("slow"), purpose="gallery", filename="timeout-slow.png")
    assert state == "processing"
    assert (await c.get(f"/uploads/{up}")).json()["state"] == "processing"
    for _ in range(60):
        await asyncio.sleep(0.1)
        att = (await c.get(f"/uploads/{up}")).json()
        if att["state"] != "processing":
            break
    assert att["state"] == "ready" and att["rating"] == "general" and att["reviewState"] == "auto"
    await c.close()


def test_http_classifier_needs_absolute_image_url(monkeypatch):
    monkeypatch.setenv("SITE_STORAGE_BACKEND", "local")
    monkeypatch.setenv("SITE_CLASSIFIER", "http")
    monkeypatch.setenv("SITE_CLASSIFIER_URL", "http://127.0.0.1:1/classify")
    monkeypatch.delenv("SITE_PUBLIC_BASE", raising=False)
    try:
        with pytest.raises(RuntimeError, match="SITE_PUBLIC_BASE"):
            reload_files_config()
    finally:
        for var in ("SITE_STORAGE_BACKEND", "SITE_CLASSIFIER", "SITE_CLASSIFIER_URL"):
            monkeypatch.delenv(var, raising=False)
        reload_files_config()


def test_map_result_edge_cases():
    from types import SimpleNamespace

    cls = HttpClassifier(
        SimpleNamespace(
            classifier_r18_threshold=0.5,
            classifier_r18g_threshold=0.5,
            classifier_confidence=0.7,
        )
    )
    assert cls.map_result({"scores": {"general": float("nan"), "r18": 0.1}}).auto_rating == "suspect"
    assert cls.map_result({"scores": {"general": 1.5}}).auto_rating == "suspect"
    assert cls.map_result({"scores": [0.9]}).auto_rating == "suspect"
    assert cls.map_result({}).auto_rating == "suspect"
    assert cls.map_result({"rating": "blocked"}).blocked
    r = cls.map_result({"rating": "r18", "scores": {"general": 0.9}})
    assert (r.auto_rating, r.rating, r.needs_review) == ("r18", "r18", False)
    r = cls.map_result({"rating": "suspect", "scores": {"r18g": 0.8}})
    assert r.needs_review and r.rating == "r18g"
    r = cls.map_result({"scores": {"general": 0.95, "r18": 0.03, "r18g": 0.02}})
    assert (r.auto_rating, r.needs_review) == ("general", False)
