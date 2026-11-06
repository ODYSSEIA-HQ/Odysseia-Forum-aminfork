"""S3 兼容存储集成测试：真的起一个 S3 服务（MinIO 或 moto server），走 /api/v1 的上传、下载全流程。

没配 SITE_S3_IT_ENDPOINT 或连不上时整个文件跳过。本地跑法（任选一个）：

  # MinIO
  minio server /tmp/minio-data --address 127.0.0.1:9100 --console-address 127.0.0.1:9101
  SITE_S3_IT_ENDPOINT=http://127.0.0.1:9100 pytest tests/site_v1/test_site_v1_s3_integration.py

  # moto（pip install 'moto[server]'）
  moto_server -H 127.0.0.1 -p 9100
  SITE_S3_IT_ENDPOINT=http://127.0.0.1:9100 pytest tests/site_v1/test_site_v1_s3_integration.py

凭证：SITE_S3_IT_ACCESS_KEY / SITE_S3_IT_SECRET_KEY（默认 minioadmin / minioadmin，moto 随便填）。
每个测试建一个随机名字的桶，结束时清空删除。

和 test_site_v1_files_api.py 的区别：直传和读取都按完整地址发真实 HTTP 请求到 S3 服务
（浏览器的做法），而不是走本服务的 /api/storage 签名端点。
"""

import asyncio
import os
import secrets
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from models.attachment import Attachment
from shared.time_utils import utc_now
from site_v1.config import reload_config
from site_v1.deps import set_session_factory
from site_v1.files_config import reload_files_config
from site_v1.fluxer import set_fluxer
from site_v1.fluxer.fake import FakeFluxer
from site_v1.services.accounts import ensure_account_rows
from site_v1.services.attachments import purge_stale_uploads
from site_v1.storage import get_storage
from site_v1.storage.s3 import S3Storage

from test_site_v1_files_api import CARD_PNG, PNG, PASSWORD, Client, assert_error
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.work import Work

ENDPOINT = os.environ.get("SITE_S3_IT_ENDPOINT", "").rstrip("/")
ACCESS = os.environ.get("SITE_S3_IT_ACCESS_KEY", "minioadmin")
SECRET = os.environ.get("SITE_S3_IT_SECRET_KEY", "minioadmin")


def _reachable() -> bool:
    if not ENDPOINT:
        return False
    try:
        httpx.get(ENDPOINT + "/", timeout=2)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="没有可用的 S3 兼容服务（设置 SITE_S3_IT_ENDPOINT，见文件头）"
)

ENV_VARS = (
    "FLUXER_FAKE",
    "SITE_REGISTRATION_MODE",
    "SITE_STORAGE_BACKEND",
    "SITE_S3_ENDPOINT",
    "SITE_S3_REGION",
    "SITE_S3_BUCKET",
    "SITE_S3_ACCESS_KEY_ID",
    "SITE_S3_SECRET_ACCESS_KEY",
    "SITE_S3_PUBLIC_BASE",
    "SITE_CLASSIFIER",
    "SITE_UPLOAD_TICKET_TTL",
)


def _boto():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest_asyncio.fixture
async def s3env(monkeypatch, db_session_factory, redis_client):
    bucket = f"it-{secrets.token_hex(6)}"
    boto = _boto()
    boto.create_bucket(Bucket=bucket)

    monkeypatch.setenv("FLUXER_FAKE", "1")
    monkeypatch.setenv("SITE_REGISTRATION_MODE", "open")
    monkeypatch.setenv("SITE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("SITE_S3_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("SITE_S3_REGION", "us-east-1")
    monkeypatch.setenv("SITE_S3_BUCKET", bucket)
    monkeypatch.setenv("SITE_S3_ACCESS_KEY_ID", ACCESS)
    monkeypatch.setenv("SITE_S3_SECRET_ACCESS_KEY", SECRET)
    monkeypatch.delenv("SITE_S3_PUBLIC_BASE", raising=False)
    monkeypatch.setenv("SITE_CLASSIFIER", "stub")
    reload_config()
    reload_files_config()
    assert isinstance(get_storage(), S3Storage)
    set_session_factory(db_session_factory)
    fake = FakeFluxer(session_factory=db_session_factory, redis=redis_client)
    set_fluxer(fake)

    from site_v1 import install

    app = FastAPI()
    install(app)

    async def account(username, *, r18=False):
        user_id = await fake.ensure_account(
            username, PASSWORD, email=f"{username}@example.com"
        )
        async with db_session_factory() as session:
            await ensure_account_rows(session, user_id)
            row = await session.get(SiteRestrictedAccess, user_id)
            row.state = "granted"
            row.age_confirmed_at = utc_now()
            row.terms_version = "2026-10"
            if r18:
                prefs = await session.get(SiteUserPrefs, user_id)
                prefs.content_mode = "r18"
            acc = await session.get(SiteAccount, user_id)
            acc.permissions = []
            await session.commit()
        return user_id

    async def work(author_id, *, status="published", rating="general"):
        async with db_session_factory() as session:
            w = Work(
                title="S3 测试作品",
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

    yield app, account, work, db_session_factory, bucket, boto

    set_session_factory(None)
    set_fluxer(None)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reload_config()
    reload_files_config()
    listed = boto.list_objects_v2(Bucket=bucket).get("Contents", [])
    for obj in listed:
        boto.delete_object(Bucket=bucket, Key=obj["Key"])
    boto.delete_bucket(Bucket=bucket)


def enforces_auth(bucket: str) -> bool:
    """服务会不会校验签名：匿名列桶被拒就算会（MinIO、R2 会；moto server 默认不校验，
    开了鉴权模式又不支持预签名地址）。不校验时，签名、过期相关的断言跳过。"""
    return httpx.get(f"{ENDPOINT}/{bucket}", timeout=5).status_code in (401, 403)


def ticket_signed_headers(ticket: dict) -> list[str]:
    from urllib.parse import parse_qs, urlsplit

    qs = parse_qs(urlsplit(ticket["url"]).query)
    return qs["X-Amz-SignedHeaders"][0].split(";")


async def direct_upload(c: Client, ticket: dict, data: bytes, headers=None):
    """浏览器直传：按 ticket 原样请求 S3（真实 HTTP，不经过本服务）。"""
    async with httpx.AsyncClient(timeout=10) as http:
        return await http.request(
            ticket["method"],
            ticket["url"],
            content=data,
            headers=ticket["headers"] if headers is None else headers,
        )


async def fetch(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as http:
        return await http.get(url)


async def new_ticket(c, wid, data, *, purpose="cover", filename="card.png", mime="image/png"):
    res = await c.post(
        "/uploads",
        {
            "purpose": purpose,
            "workId": str(wid),
            "filename": filename,
            "size": len(data),
            "mime": mime,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


@pytest.mark.asyncio
async def test_s3_direct_upload_keeps_png_text_chunk(s3env):
    app, account, work, factory, bucket, boto = s3env
    uid = await account("author")
    wid = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")

    ticket = await new_ticket(c, wid, CARD_PNG, filename="角色卡 card.png")
    assert ticket["method"] == "PUT" and ticket["url"].startswith(ENDPOINT + "/" + bucket + "/")
    assert ticket["headers"] == {"Content-Type": "image/png"}

    # 还没传：complete 409
    assert_error(await c.post(f"/uploads/{ticket['uploadId']}/complete"), 409, "conflict")

    put = await direct_upload(c, ticket, CARD_PNG)
    assert put.status_code == 200, put.text
    done = await c.post(f"/uploads/{ticket['uploadId']}/complete")
    assert done.json() == {"state": "ready"}, done.text

    att = (await c.get(f"/uploads/{ticket['uploadId']}")).json()
    assert att["state"] == "ready" and (att["width"], att["height"]) == (1, 1)
    assert att["url"].startswith(ENDPOINT + "/")
    got = await fetch(att["url"])
    assert got.status_code == 200
    assert got.content == CARD_PNG  # 字节原样，tEXt chara 还在
    assert b"tEXtchara" in got.content
    assert got.headers["content-type"].startswith("image/png")

    # 对象本身：Content-Type 存对，sha256 记下
    async with factory() as session:
        row = await session.get(Attachment, int(ticket["uploadId"]))
        key = row.storage_key
        assert row.sha256 and len(row.sha256) == 64
    head = boto.head_object(Bucket=bucket, Key=key)
    assert head["ContentType"] == "image/png" and head["ContentLength"] == len(CARD_PNG)
    await c.close()


@pytest.mark.asyncio
async def test_s3_upload_checks(s3env):
    app, account, work, factory, bucket, boto = s3env
    uid = await account("author")
    wid = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")

    # 传的字节数和申请时不一样：Content-Length 签进了地址，校验签名的服务直接 403；
    # 不校验的服务（moto）收下了，complete 时 409 size_mismatch 兜底
    strict = enforces_auth(bucket)
    assert "content-length" in ticket_signed_headers(await new_ticket(c, wid, PNG))
    ticket = await new_ticket(c, wid, PNG)
    put = await direct_upload(c, ticket, PNG + b"extra")
    if strict:
        assert put.status_code == 403, put.text
        assert_error(await c.post(f"/uploads/{ticket['uploadId']}/complete"), 409, "conflict")
    else:
        assert put.status_code == 200
        err = assert_error(await c.post(f"/uploads/{ticket['uploadId']}/complete"), 409, "conflict")
        assert err["details"]["reason"] == "size_mismatch"
    # 重传正确的字节后可以 complete（同一个 ticket 有效期内可重试）
    assert (await direct_upload(c, ticket, PNG)).status_code == 200
    assert (await c.post(f"/uploads/{ticket['uploadId']}/complete")).json() == {"state": "ready"}

    if not strict:
        await c.close()
        pytest.skip("这个 S3 服务不校验签名（moto），签名相关断言跳过")
    # Content-Type 参与签名：换类型直传被 S3 拒绝
    t1 = await new_ticket(c, wid, PNG)
    bad = await direct_upload(c, t1, PNG, headers={"Content-Type": "image/jpeg"})
    assert bad.status_code == 403, bad.text
    assert_error(await c.post(f"/uploads/{t1['uploadId']}/complete"), 409, "conflict")
    # 地址被改：签名不对
    t2 = await new_ticket(c, wid, PNG)
    tampered = dict(t2, url=t2["url"].replace("X-Amz-Signature=", "X-Amz-Signature=00"))
    assert (await direct_upload(c, tampered, PNG)).status_code == 403
    # 读取地址不带签名：拒绝
    async with factory() as session:
        key = (await session.get(Attachment, int(ticket["uploadId"]))).storage_key
    assert (await fetch(f"{ENDPOINT}/{bucket}/{key}")).status_code == 403
    await c.close()


@pytest.mark.asyncio
async def test_s3_ticket_expires(s3env, monkeypatch):
    app, account, work, factory, bucket, boto = s3env
    if not enforces_auth(bucket):
        pytest.skip("这个 S3 服务不校验签名和过期（moto）")
    monkeypatch.setenv("SITE_UPLOAD_TICKET_TTL", "1")
    reload_files_config()
    uid = await account("author")
    wid = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")
    ticket = await new_ticket(c, wid, PNG)
    await asyncio.sleep(2.2)
    res = await direct_upload(c, ticket, PNG)
    assert res.status_code == 403, res.text
    await c.close()


@pytest.mark.asyncio
async def test_s3_version_download_redirect_and_r18_url(s3env):
    app, account, work, factory, bucket, boto = s3env
    uid = await account("author", r18=True)
    await account("reader")
    wid = await work(uid)
    c = Client(app)
    await c.login("author")

    data = b'{"spec":"chara_card_v2","data":{"name":"\xe5\xb0\x8f\xe6\x98\x8e"}}'
    t = await new_ticket(
        c, wid, data, purpose="version_file", filename="小明 v1.json", mime="application/json"
    )
    assert (await direct_upload(c, t, data)).status_code == 200
    assert (await c.post(f"/uploads/{t['uploadId']}/complete")).json() == {"state": "ready"}
    # 角色卡 PNG 作为版本文件
    t2 = await new_ticket(c, wid, CARD_PNG, purpose="version_file", filename="card.png")
    assert (await direct_upload(c, t2, CARD_PNG)).status_code == 200
    assert (await c.post(f"/uploads/{t2['uploadId']}/complete")).json() == {"state": "ready"}

    v = await c.post(
        f"/works/{wid}/versions", {"name": "v1", "fileIds": [t["uploadId"], t2["uploadId"]]}
    )
    assert v.status_code == 201, v.text
    vid = v.json()["id"]

    reader = Client(app)
    await reader.login("reader")
    for fid, body, ctype in ((t["uploadId"], data, "application/json"), (t2["uploadId"], CARD_PNG, "image/png")):
        res = await reader.get(f"/works/{wid}/versions/{vid}/files/{fid}/download")
        assert res.status_code == 302, res.text
        location = res.headers["location"]
        assert location.startswith(ENDPOINT + "/") and "X-Amz-Signature=" in location
        assert "X-Amz-Expires=600" in location  # 协议：10 分钟
        got = await fetch(location)
        assert got.status_code == 200
        assert got.content == body
        assert got.headers["content-type"].startswith(ctype)
        disposition = got.headers["content-disposition"]
        assert disposition.startswith("attachment")
    # 中文文件名按 RFC 5987 编码
    res = await reader.get(f"/works/{wid}/versions/{vid}/files/{t['uploadId']}/download")
    got = await fetch(res.headers["location"])
    assert "UTF-8''%E5%B0%8F%E6%98%8E%20v1.json" in got.headers["content-disposition"]

    # R18 作品图：作者（R18 模式）拿到 10 分钟签名地址
    r18w = await work(uid, rating="r18")
    t3 = await new_ticket(c, r18w, PNG, purpose="gallery", filename="x-r18.png")
    assert (await direct_upload(c, t3, PNG)).status_code == 200
    assert (await c.post(f"/uploads/{t3['uploadId']}/complete")).json() == {"state": "ready"}
    att = (await c.get(f"/uploads/{t3['uploadId']}")).json()
    assert att["rating"] == "r18" and "X-Amz-Expires=600" in att["url"]
    assert (await fetch(att["url"])).content == PNG
    for x in (c, reader):
        await x.close()


@pytest.mark.asyncio
async def test_s3_public_base_for_general_images(s3env, monkeypatch):
    app, account, work, factory, bucket, boto = s3env
    monkeypatch.setenv("SITE_S3_PUBLIC_BASE", "https://img.example.com/")
    reload_files_config()
    uid = await account("author")
    wid = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")
    t = await new_ticket(c, wid, PNG, filename="a b.png")
    assert (await direct_upload(c, t, PNG)).status_code == 200
    assert (await c.post(f"/uploads/{t['uploadId']}/complete")).json() == {"state": "ready"}
    att = (await c.get(f"/uploads/{t['uploadId']}")).json()
    async with factory() as session:
        key = (await session.get(Attachment, int(t["uploadId"]))).storage_key
    assert att["url"] == f"https://img.example.com/{key}"
    await c.close()


@pytest.mark.asyncio
async def test_s3_purge_stale_uploads(s3env):
    """申请了直传却一直没 complete 的附件：过期后删对象、标记删除。"""
    app, account, work, factory, bucket, boto = s3env
    uid = await account("author")
    wid = await work(uid, status="draft")
    c = Client(app)
    await c.login("author")
    stale = await new_ticket(c, wid, PNG, filename="stale.png")
    assert (await direct_upload(c, stale, PNG)).status_code == 200  # 传了但没 complete
    never = await new_ticket(c, wid, PNG, filename="never.png")  # 根本没传
    fresh = await new_ticket(c, wid, PNG, filename="fresh.png")
    assert (await direct_upload(c, fresh, PNG)).status_code == 200

    async with factory() as session:
        keys = {}
        for t in (stale, never):
            row = await session.get(Attachment, int(t["uploadId"]))
            row.created_at = utc_now() - timedelta(days=2)
            keys[t["uploadId"]] = row.storage_key
        fresh_key = (await session.get(Attachment, int(fresh["uploadId"]))).storage_key
        await session.commit()

    assert await get_storage().stat(keys[stale["uploadId"]]) == len(PNG)
    purged = await purge_stale_uploads()
    assert purged == 2
    assert await get_storage().stat(keys[stale["uploadId"]]) is None
    assert await get_storage().stat(fresh_key) == len(PNG)
    async with factory() as session:
        for t in (stale, never):
            row = await session.get(Attachment, int(t["uploadId"]))
            assert row.deleted_at is not None
    assert_error(await c.get(f"/uploads/{stale['uploadId']}"), 404, "not_found")
    # 幂等
    assert await purge_stale_uploads() == 0
    # 删对象：不存在也不报错
    await get_storage().delete(keys[stale["uploadId"]])
    await c.close()


@pytest.mark.asyncio
async def test_s3_cors_preflight_with_documented_rules(s3env):
    """docs/site_v1/r2-cors.json（README 里给 R2 的 CORS 配置）：浏览器直传的预检要放行。"""
    import json
    from pathlib import Path

    app, account, work, factory, bucket, boto = s3env
    rules = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/site_v1/r2-cors.json").read_text()
    )
    boto.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": rules})
    origin = rules[0]["AllowedOrigins"][0]
    async with httpx.AsyncClient(timeout=5) as http:
        res = await http.options(
            f"{ENDPOINT}/{bucket}/att/x/a.png",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert res.status_code == 200, res.text
    assert res.headers["access-control-allow-origin"] == origin
    assert "PUT" in res.headers["access-control-allow-methods"]
    assert "content-type" in res.headers["access-control-allow-headers"].lower()
    # 不在名单里的域名被拒
    async with httpx.AsyncClient(timeout=5) as http:
        res = await http.options(
            f"{ENDPOINT}/{bucket}/att/x/a.png",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"},
        )
    assert res.status_code == 403
