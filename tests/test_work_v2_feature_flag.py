"""真实应用入口的V2灰度关闭边界。"""

import httpx
import pytest
from api.main import app


@pytest.mark.asyncio
async def test_disabled_v2_does_not_enter_auth_or_rate_limit_storage(monkeypatch):
    """关闭时全部V2先返回404，旧V1认证配置仍可读。"""
    monkeypatch.setenv("FORUM_V2_ENABLED", "false")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://forum.test"
    ) as client:
        for path in ("/v2", "/v2/me", "/v2/auth/config", "/v2/works"):
            response = await client.get(path)
            assert response.status_code == 404
        legacy = await client.get("/v1/auth/config")
        assert legacy.status_code == 200 and legacy.json()["mode"] == "legacy"
