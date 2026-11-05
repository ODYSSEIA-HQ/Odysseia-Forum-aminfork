"""Fluxer 浏览器公开 API 地址配置测试。"""

import httpx
import pytest
from fastapi import FastAPI

import api.v1.routers.auth as auth_module
from shared.platform_thread_ref import fluxer_thread_ref


@pytest.fixture
def auth_app(monkeypatch):
    """创建挂载 V1 和 V2 认证路由的最小应用。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/v1")
    app.include_router(auth_module.router, prefix="/v2")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("/v1/auth/config", "/v2/auth/config"))
async def test_internal_fluxer_url_is_not_returned_without_public_url(
    auth_app, monkeypatch, path
):
    """未配置浏览器地址时不回退公开内网集成地址。"""
    internal_url = "http://10.23.4.5:8080/api"
    monkeypatch.setenv("FLUXER_API_URL", internal_url)
    monkeypatch.delenv("FLUXER_PUBLIC_API_URL", raising=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://forum.test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.json()["fluxer_api_url"] == ""
    assert internal_url not in response.text
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
async def test_valid_public_https_url_is_returned_without_rewriting_internal_url(
    auth_app, monkeypatch
):
    """只返回独立配置的 HTTPS 地址，保留内部集成配置。"""
    internal_url = "http://10.23.4.5:8080/api"
    public_url = "https://api.example.com:8443/fluxer/api"
    monkeypatch.setenv("FLUXER_API_URL", internal_url)
    monkeypatch.setenv("FLUXER_PUBLIC_API_URL", public_url)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://forum.test"
    ) as client:
        response = await client.get("/v2/auth/config")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "fluxer",
        "fluxer_api_url": public_url,
    }
    assert internal_url not in response.text
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "public_url",
    (
        "",
        "   ",
        "api.example.com",
        "//api.example.com",
        "ftp://api.example.com",
        "https://",
        "http://api.example.com",
        "http://localhost:8080",
        "https://localhost",
        "https://api.localhost",
        "https://user:placeholder@api.example.com",
        "https://api.example.com?tenant=1",
        "https://api.example.com#fragment",
        "https://api.example.com:not-a-port",
        "https://api.example.com:0",
        "https://api.example.com:65536",
        "https://api.example.com:",
        "https://10.23.4.5:8080",
        "https://127.0.0.1",
        "https://[::1]",
        "https://2130706433",
        "https://fluxer.internal",
        "https://fluxer.local",
        "https://api.example.com\\path",
    ),
)
async def test_invalid_public_url_is_withheld(
    auth_app, monkeypatch, public_url
):
    """拒绝非 HTTPS、含凭据或指向非公开地址的配置。"""
    internal_url = "http://10.23.4.5:8080/api"
    monkeypatch.setenv("FLUXER_API_URL", internal_url)
    monkeypatch.setenv("FLUXER_PUBLIC_API_URL", public_url)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app), base_url="http://forum.test"
    ) as client:
        response = await client.get("/v2/auth/config")

    assert response.status_code == 200
    assert response.json()["fluxer_api_url"] == ""
    assert internal_url not in response.text


def test_internal_fluxer_url_remains_available_for_thread_links(monkeypatch):
    """保留后端既有内网基址回退行为。"""
    internal_url = "http://10.23.4.5:8080/api"
    monkeypatch.setenv("FLUXER_API_URL", internal_url)
    monkeypatch.delenv("FLUXER_WEB_URL", raising=False)

    thread_url = fluxer_thread_ref("guild", "thread")

    assert thread_url == "http://10.23.4.5:8080/channels/guild/thread"
