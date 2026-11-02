"""旧 Discord 协议明确排除不能表达的原生作品 Banner。"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
import pytest
from api.v1.routers import banner as router
from banner.banner_service import BannerService
from models import BannerCarousel
from shared.time_utils import utc_now
from banner.views.banner_status_view import BannerStatusView
from banner.dto.banner_scope_status import BannerScopeStatus
from banner.dto.banner_status_item import BannerStatusItem


@pytest.mark.asyncio
async def test_native_work_is_excluded_from_v1_without_fake_discord_id(monkeypatch):
    """V1 没有 Work 定位字段，不能返回空或虚构 Discord thread_id。"""
    monkeypatch.setattr(router, "async_session_factory", MagicMock())
    row = BannerCarousel(
        thread_id=None,
        work_id=9007199254740993123,
        target_type=3,
        title="原生作品",
        end_time=utc_now() + timedelta(days=1),
    )
    monkeypatch.setattr(
        BannerService, "get_active_banners", AsyncMock(return_value=[row])
    )
    assert await router.get_active_banners(None, None, {}) == []


def test_bot_status_uses_explicit_work_type():
    """状态保留容量占位并显示作品编号，不伪造 Discord 跳转。"""
    now = utc_now()
    scopes = [
        BannerScopeStatus(
            None,
            3,
            0,
            [
                BannerStatusItem(
                    9007199254740993123, 3, "原生作品", now + timedelta(days=1)
                )
            ],
        )
    ]
    text = BannerStatusView.build_messages(scopes, {}, now)[0]
    assert "[作品]" in text
    assert "9007199254740993123" in text
    assert "discord.com" not in text
