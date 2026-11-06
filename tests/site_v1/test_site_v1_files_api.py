"""/api/v1 上传、版本、下载：本地存储 + 桩分级器 + FakeFluxer + create_all 的测试库。

作品行直接插 Work 表（不依赖作品领域的接口）。
"""

import base64
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from models.attachment import Attachment
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.work import Work
from models.work_statistics import WorkStatistics
from shared.time_utils import utc_now
from site_v1.classifier import HttpClassifier, image_size
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.files_config import get_files_config, reload_files_config
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.services.accounts import ensure_account_rows
from site_v1.services.attachments import apply_review

BASE = "https://site.test"
PASSWORD = "demo1234"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
# 带 tEXt 块的 PNG（角色卡）：必须原样保存
CARD_PNG = PNG[:33] + b"\x00\x00\x00\x09tEXtchara\x00abc\x00\x00\x00\x00" + PNG[33:]


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path, db_session_factory, redis_client):
    monkeypatch.setenv("FLUXER_FAKE", "1")
    monkeypatch.setenv("SITE_REGISTRATION_MODE", "open")
    monkeypatch.setenv("SITE_STORAGE_BACKEND", "local")
    monkeypatch.setenv("SITE_STORAGE_LOCAL_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SITE_STORAGE_SIGNING_KEY", "test-key")
    monkeypatch.setenv("SITE_CLASSIFIER", "stub")
    monkeypatch.delenv("SITE_PUBLIC_BASE", raising=False)
    reload_config()
    reload_files_config()
    set_session_factory(db_session_factory)
    fake = FakeFluxer(session_factory=db_session_factory, redis=redis_client)
    set_fluxer(fake)

    from site_v1 import install

    app = FastAPI()
    install(app)

    async def account(username, *, access="none", permissions=(), r18=False):
        user_id = await fake.ensure_account(
            username, PASSWORD, email=f"{username}@example.com"
        )
        async with db_session_factory() as session:
            await ensure_account_rows(session, user_id)
            row = await session.get(SiteRestrictedAccess, user_id)
            row.state = access
            if access == "granted":
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
                title="测试作品",
                author_id=author_id,
                status=status,
                is_nsfw=rating != "general",
                is_abyss=rating == "r18g",
                visibility="public" if rating == "general" else "restricted",
            )
            session.add(w)
            await session.flush()
            work_id = w.id
            await session.commit()
            return work_id

    yield app, account, work, db_session_factory
    set_session_factory(None)
    set_fluxer(None)
    for var in (
        "FLUXER_FAKE",
        "SITE_REGISTRATION_MODE",
        "SITE_STORAGE_BACKEND",
        "SITE_STORAGE_LOCAL_DIR",
        "SITE_STORAGE_SIGNING_KEY",
        "SITE_CLASSIFIER",
    ):
        monkeypatch.delenv(var, raising=False)
    reload_config()
    reload_files_config()


class Client:
    def __init__(self, app):
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        )
        self.csrf = None

    async def session(self):
        res = await self.http.get("/api/v1/session")
        self.csrf = res.json()["csrfToken"]

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
        res = await self.post(
            "/auth/login", {"username": username, "password": PASSWORD}
        )
        assert res.status_code == 200, res.text
        await self.session()

    async def raw(self, method, url, **kw):
        """按完整地址请求（直传、签名读取）：只取路径和查询串，走同一个 app。"""
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        return await self.http.request(method, target, **kw)

    async def upload(
        self, work_id, data=PNG, *, purpose="cover", filename="c.png", mime="image/png"
    ):
        body = {
            "purpose": purpose,
            "filename": filename,
            "size": len(data),
            "mime": mime,
        }
        if work_id is not None:
            body["workId"] = str(work_id)
        res = await self.post("/uploads", body)
        assert res.status_code == 201, res.text
        ticket = res.json()
        put = await self.raw(
            ticket["method"], ticket["url"], content=data, headers=ticket["headers"]
        )
        assert put.status_code < 300, put.text
        done = await self.post(f"/uploads/{ticket['uploadId']}/complete")
        assert done.status_code == 200, done.text
        return ticket["uploadId"], done.json()["state"]

    async def close(self):
        await self.http.aclose()


def assert_error(res, status, code):
    assert res.status_code == status, res.text
    body = res.json()
    assert body["error"]["code"] == code
    return body["error"]


@pytest.mark.asyncio
async def test_upload_full_flow_keeps_bytes(env):
    app, account, work, _ = env
    uid = await account("author", access="granted")
    wid = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")

    res = await c.post(
        "/uploads",
        {
            "purpose": "cover",
            "workId": str(wid),
            "filename": "card.png",
            "size": len(CARD_PNG),
            "mime": "image/png",
        },
    )
    assert res.status_code == 201, res.text
    ticket = res.json()
    assert set(ticket) == {"uploadId", "method", "url", "headers"}
    assert ticket["method"] == "PUT" and ticket["url"].startswith(
        BASE + "/api/storage/upload/"
    )
    assert ticket["headers"] == {"Content-Type": "image/png"}

    # 还没传：complete 409，GET 409
    assert_error(
        await c.post(f"/uploads/{ticket['uploadId']}/complete"), 409, "conflict"
    )
    assert_error(await c.get(f"/uploads/{ticket['uploadId']}"), 409, "conflict")

    put = await c.raw("PUT", ticket["url"], content=CARD_PNG, headers=ticket["headers"])
    assert put.status_code == 200
    done = await c.post(f"/uploads/{ticket['uploadId']}/complete")
    assert done.json() == {"state": "ready"}
    # 再 complete 一次：幂等
    assert (await c.post(f"/uploads/{ticket['uploadId']}/complete")).json() == {
        "state": "ready"
    }

    att = (await c.get(f"/uploads/{ticket['uploadId']}")).json()
    assert att["id"] == ticket["uploadId"]
    assert att["state"] == "ready" and att["rating"] == "general"
    assert att["kind"] == "image" and att["placeholder"] is False
    assert att["reviewState"] == "auto" and att["rejectReason"] is None
    assert (att["width"], att["height"]) == (1, 1)
    assert att["url"] and att["thumbUrl"] == att["url"]
    got = await c.raw("GET", att["url"])
    assert got.status_code == 200 and got.content == CARD_PNG  # 原样，tEXt 还在
    assert got.headers["content-type"].startswith("image/png")
    await c.close()


@pytest.mark.asyncio
async def test_upload_validation_and_permissions(env):
    app, account, work, _ = env
    uid = await account("author")
    await account("other")
    wid = await work(uid, status="draft")

    anon = Client(app)
    assert_error(
        await anon.post(
            "/uploads",
            {"purpose": "avatar", "filename": "a.png", "size": 1, "mime": "image/png"},
        ),
        401,
        "unauthenticated",
    )

    c = Client(app)
    await c.login("author")
    err = assert_error(
        await c.post(
            "/uploads",
            {
                "purpose": "cover",
                "workId": str(wid),
                "filename": "x.exe",
                "size": 10,
                "mime": "application/x-msdownload",
            },
        ),
        422,
        "validation_failed",
    )
    assert "mime" in err["details"]["fields"]
    err = assert_error(
        await c.post(
            "/uploads",
            {
                "purpose": "cover",
                "workId": str(wid),
                "filename": "a.zip",
                "size": 10,
                "mime": "application/zip",
            },
        ),
        422,
        "validation_failed",
    )
    assert "mime" in err["details"]["fields"]
    err = assert_error(
        await c.post(
            "/uploads",
            {
                "purpose": "gallery",
                "workId": str(wid),
                "filename": "a.png",
                "size": 999_999_999,
                "mime": "image/png",
            },
        ),
        422,
        "validation_failed",
    )
    assert "size" in err["details"]["fields"]
    err = assert_error(
        await c.post(
            "/uploads",
            {
                "purpose": "cover",
                "workId": "999999",
                "filename": "a.png",
                "size": 10,
                "mime": "image/png",
            },
        ),
        422,
        "validation_failed",
    )
    assert "workId" in err["details"]["fields"]
    err = assert_error(
        await c.post(
            "/uploads",
            {"purpose": "nope", "filename": "", "size": 0, "mime": "image/png"},
        ),
        422,
        "validation_failed",
    )
    assert {"purpose", "filename", "size"} <= set(err["details"]["fields"])

    upload_id, state = await c.upload(wid)
    assert state == "ready"

    other = Client(app)
    await other.login("other")
    assert_error(
        await other.post(
            "/uploads",
            {
                "purpose": "cover",
                "workId": str(wid),
                "filename": "a.png",
                "size": 10,
                "mime": "image/png",
            },
        ),
        403,
        "forbidden",
    )
    assert_error(await other.get(f"/uploads/{upload_id}"), 404, "not_found")
    assert_error(await other.post(f"/uploads/{upload_id}/complete"), 404, "not_found")
    assert_error(await c.get("/uploads/abc"), 404, "not_found")
    for x in (anon, c, other):
        await x.close()


@pytest.mark.asyncio
async def test_signed_put_checks(env):
    app, account, work, _ = env
    uid = await account("author")
    wid = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")
    ticket = (
        await c.post(
            "/uploads",
            {
                "purpose": "cover",
                "workId": str(wid),
                "filename": "a.png",
                "size": len(PNG),
                "mime": "image/png",
            },
        )
    ).json()
    # 大小不对、类型不对、令牌被改
    assert (
        await c.raw("PUT", ticket["url"], content=PNG + b"x", headers=ticket["headers"])
    ).status_code == 400
    assert (
        await c.raw(
            "PUT", ticket["url"], content=PNG, headers={"Content-Type": "image/jpeg"}
        )
    ).status_code == 400
    assert (
        await c.raw("PUT", ticket["url"] + "x", content=PNG, headers=ticket["headers"])
    ).status_code == 403
    # 失败的直传不留下对象：complete 仍然 409
    assert_error(
        await c.post(f"/uploads/{ticket['uploadId']}/complete"), 409, "conflict"
    )
    assert (
        await c.raw("GET", BASE + "/api/storage/files/bogus.token")
    ).status_code == 403
    await c.close()


@pytest.mark.asyncio
async def test_classification_outcomes(env):
    app, account, work, factory = env
    uid = await account("author", access="granted")
    published = await work(uid)
    draft = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")

    # 已发布的全年龄作品传 R18 图：分级冲突
    up, state = await c.upload(published, filename="pic-r18.png")
    assert state == "rejected"
    att = (await c.get(f"/uploads/{up}")).json()
    assert att["rejectReason"] == "rating_conflict" and att["url"] is None

    # 违禁
    up, state = await c.upload(draft, filename="blocked.png")
    att = (await c.get(f"/uploads/{up}")).json()
    assert state == "rejected" and att["rejectReason"] == "blocked"

    # 草稿还没定分级：R18 图先收下；全年龄模式下看不到（占位）
    up, state = await c.upload(draft, purpose="gallery", filename="pic-r18.png")
    att = (await c.get(f"/uploads/{up}")).json()
    assert state == "ready" and att["rating"] == "r18"
    assert att["placeholder"] is True and att["url"] is None

    # 拿不准：保守按 R18、待人工审核
    up, state = await c.upload(published, filename="maybe-suspect.png")
    att = (await c.get(f"/uploads/{up}")).json()
    assert (
        state == "ready"
        and att["rating"] == "r18"
        and att["reviewState"] == "pending_review"
    )
    async with factory() as session:
        row = await session.get(Attachment, int(up))
        assert row.auto_rating == "suspect" and row.model_scores
        apply_review(row, rating="general")
        await session.commit()
    att = (await c.get(f"/uploads/{up}")).json()
    assert (
        att["rating"] == "general" and att["reviewState"] == "reviewed" and att["url"]
    )

    # 非图片不过模型，按作品分级
    up, state = await c.upload(
        published,
        purpose="version_file",
        filename="card.json",
        data=b'{"a":1}',
        mime="application/json",
    )
    att = (await c.get(f"/uploads/{up}")).json()
    assert state == "ready" and att["kind"] == "file" and att["rating"] == "general"
    await c.close()


@pytest.mark.asyncio
async def test_r18_image_url_for_r18_viewer(env):
    app, account, work, _ = env
    uid = await account("author", access="granted", r18=True)
    wid = await work(uid, rating="r18")
    c = Client(app)
    await c.login("author")
    up, state = await c.upload(wid, filename="x-r18.png")
    att = (await c.get(f"/uploads/{up}")).json()
    assert state == "ready" and att["rating"] == "r18" and att["placeholder"] is False
    assert (await c.raw("GET", att["url"])).content == PNG
    await c.close()


@pytest.mark.asyncio
async def test_versions_and_download(env):
    app, account, work, factory = env
    uid = await account("author", access="granted")
    await account("plain")
    wid = await work(uid)
    c = Client(app)
    await c.login("author")
    file_id, _ = await c.upload(
        wid,
        purpose="version_file",
        filename="card.json",
        data=b'{"name":"x"}',
        mime="application/json",
    )
    cover_id, _ = await c.upload(wid)

    res = await c.post(
        f"/works/{wid}/versions",
        {
            "name": "v1",
            "changelog": "初版",
            "license": "仅测试",
            "fileIds": [file_id],
            "dependencies": [
                {"name": "某插件", "url": "https://x.test", "workId": None}
            ],
        },
    )
    assert res.status_code == 201, res.text
    v1 = res.json()
    assert v1["workId"] == str(wid) and v1["name"] == "v1" and v1["license"] == "仅测试"
    assert v1["dependencies"] == [
        {"workId": None, "url": "https://x.test", "name": "某插件"}
    ]
    (f,) = v1["files"]
    assert f["id"] == file_id and f["kind"] == "file"
    assert (
        f["url"] == f"/api/v1/works/{wid}/versions/{v1['id']}/files/{file_id}/download"
    )

    # 校验：同名、名字太长、封面不能当版本文件、依赖格式
    err = assert_error(
        await c.post(f"/works/{wid}/versions", {"name": "v1"}), 422, "validation_failed"
    )
    assert "name" in err["details"]["fields"]
    assert_error(
        await c.post(f"/works/{wid}/versions", {"name": "x" * 41}),
        422,
        "validation_failed",
    )
    err = assert_error(
        await c.post(f"/works/{wid}/versions", {"name": "v9", "fileIds": [cover_id]}),
        422,
        "validation_failed",
    )
    assert "fileIds" in err["details"]["fields"]
    assert_error(
        await c.post(
            f"/works/{wid}/versions", {"name": "v9", "dependencies": [{"url": "x"}]}
        ),
        422,
        "validation_failed",
    )

    # 不带 fileIds：沿用上一版文件和授权
    v2 = (await c.post(f"/works/{wid}/versions", {"name": "v2"})).json()
    assert [x["id"] for x in v2["files"]] == [file_id] and v2["license"] == "仅测试"

    anon = Client(app)
    page = (await anon.get(f"/works/{wid}/versions")).json()
    assert [v["id"] for v in page["items"]] == [v2["id"], v1["id"]] and page[
        "nextCursor"
    ] is None
    first = (await anon.get(f"/works/{wid}/versions", params={"limit": 1})).json()
    assert [v["id"] for v in first["items"]] == [v2["id"]] and first["nextCursor"]
    second = (
        await anon.get(
            f"/works/{wid}/versions", params={"limit": 1, "cursor": first["nextCursor"]}
        )
    ).json()
    assert [v["id"] for v in second["items"]] == [v1["id"]] and second[
        "nextCursor"
    ] is None
    assert_error(
        await anon.get(f"/works/{wid}/versions", params={"cursor": "!!"}),
        422,
        "validation_failed",
    )
    assert_error(await anon.get("/works/999999/versions"), 404, "not_found")

    plain = Client(app)
    await plain.login("plain")
    assert_error(
        await plain.post(f"/works/{wid}/versions", {"name": "x"}), 403, "forbidden"
    )

    # 下载：要登录；302 到签名地址；计数
    path = f"/works/{wid}/versions/{v1['id']}/files/{file_id}/download"
    assert_error(await anon.get(path), 401, "unauthenticated")
    res = await plain.get(path)
    assert res.status_code == 302, res.text
    location = res.headers["location"]
    assert "/api/storage/files/" in location
    got = await plain.raw("GET", location)
    assert got.content == b'{"name":"x"}'
    assert "attachment" in got.headers["content-disposition"]
    await plain.get(path)
    async with factory() as session:
        stats = await session.get(WorkStatistics, wid)
        assert stats.download_count == 2
    assert_error(
        await plain.get(f"/works/{wid}/versions/{v1['id']}/files/{cover_id}/download"),
        404,
        "not_found",
    )
    assert_error(
        await plain.get(f"/works/{wid}/versions/abc/files/{file_id}/download"),
        404,
        "not_found",
    )
    for x in (c, anon, plain):
        await x.close()


@pytest.mark.asyncio
async def test_version_visibility(env):
    app, account, work, _ = env
    uid = await account("author", access="granted")
    await account("viewer", access="granted", r18=True)
    r18 = await work(uid, rating="r18")
    draft = await work(uid, status="draft")

    c = Client(app)
    await c.login("author")  # 全年龄模式
    anon = Client(app)
    # R18 作品：全年龄模式下谁都 404，作者也一样（列表）；但作者能发版本
    assert_error(await anon.get(f"/works/{r18}/versions"), 404, "not_found")
    assert_error(await c.get(f"/works/{r18}/versions"), 404, "not_found")
    assert (await c.post(f"/works/{r18}/versions", {"name": "v1"})).status_code == 201
    viewer = Client(app)
    await viewer.login("viewer")  # R18 模式
    assert len((await viewer.get(f"/works/{r18}/versions")).json()["items"]) == 1
    # 别人在 R18 模式下发版本：403；在全年龄模式下：404（看不到）
    assert_error(
        await viewer.post(f"/works/{r18}/versions", {"name": "v2"}), 403, "forbidden"
    )

    # 草稿：只有作者看得到，草稿也能发版本
    assert (await c.post(f"/works/{draft}/versions", {"name": "v1"})).status_code == 201
    assert len((await c.get(f"/works/{draft}/versions")).json()["items"]) == 1
    assert_error(await viewer.get(f"/works/{draft}/versions"), 404, "not_found")
    assert_error(
        await viewer.post(f"/works/{draft}/versions", {"name": "v2"}), 404, "not_found"
    )
    for x in (c, anon, viewer):
        await x.close()


@pytest.mark.asyncio
async def test_seed_version_and_latest(env):
    app, account, work, factory = env
    uid = await account("author")
    wid = await work(uid)
    from site_v1.context import SiteContext
    from site_v1.services.versions import latest_versions, seed_version_with_file
    from site_v1.storage import get_storage

    vid, att_id = await seed_version_with_file(factory, work_id=wid, owner_id=uid)
    assert await seed_version_with_file(factory, work_id=wid, owner_id=uid) == (
        vid,
        att_id,
    )
    async with factory() as session:
        att = await session.get(Attachment, att_id)
        assert await get_storage().stat(att.storage_key) == att.size
        latest = await latest_versions(session, [wid, 999999], SiteContext())
    assert set(latest) == {wid} and latest[wid]["id"] == str(vid)
    assert latest[wid]["files"][0]["id"] == str(att_id)

    # 种子作品能被下载
    await account("plain")
    plain = Client(app)
    await plain.login("plain")
    res = await plain.get(f"/works/{wid}/versions/{vid}/files/{att_id}/download")
    assert res.status_code == 302
    assert (await plain.raw("GET", res.headers["location"])).status_code == 200
    await plain.close()


def test_image_size_and_http_mapping():
    assert image_size(PNG) == (1, 1)
    gif = (
        b"GIF89a" + (3).to_bytes(2, "little") + (5).to_bytes(2, "little") + b"\x00" * 10
    )
    assert image_size(gif) == (3, 5)
    jpeg = (
        b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x11\x08"
        + (7).to_bytes(2, "big")
        + (9).to_bytes(2, "big")
        + b"\x03"
        + b"\x00" * 12
    )
    assert image_size(jpeg) == (9, 7)
    assert image_size(b"nope") is None

    cfg = get_files_config()
    clf = HttpClassifier(cfg)
    assert (
        clf.map_result({"scores": {"general": 0.95, "r18": 0.04, "r18g": 0.01}}).rating
        == "general"
    )
    r = clf.map_result({"scores": {"general": 0.1, "r18": 0.85, "r18g": 0.05}})
    assert r.rating == "r18" and not r.needs_review
    r = clf.map_result({"scores": {"general": 0.45, "r18": 0.5, "r18g": 0.05}})
    assert r.auto_rating == "suspect" and r.rating == "r18" and r.needs_review
    assert clf.map_result({"blocked": True}).blocked
    assert clf.map_result("garbage").needs_review
