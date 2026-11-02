"""身份模式边界的隔离测试，不连接用户数据库。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.v1.routers.booklists import _is_booklist_owner, publish_booklist
from api.v1.schemas.booklist.booklist_publish_request import BooklistPublishRequest
from identity.business_identity import resolve_business_id
from identity.principal import current_identity_id


def test_owner_identity_spaces_do_not_collide(monkeypatch):
    """相同数字的 Discord 和 Fluxer ID 不得互相取得书单所有权。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    booklist = SimpleNamespace(owner_id=123, fluxer_owner_id="456")
    assert not _is_booklist_owner(booklist, "123")
    assert _is_booklist_owner(booklist, "456")
    monkeypatch.setenv("FORUM_AUTH_MODE", "legacy")
    assert _is_booklist_owner(booklist, 123)
    assert not _is_booklist_owner(booklist, 456)


@pytest.mark.asyncio
async def test_bot_mapping_is_required(monkeypatch):
    """机器人 Discord ID 必须使用本地绑定，缺失时不伪造用户。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    lookup = AsyncMock(return_value={"123": "456"})
    monkeypatch.setattr(
        "identity.identity_repository.IdentityRepository.discord_to_fluxer", lookup
    )
    assert await resolve_business_id(object(), 123) == "456"
    assert await resolve_business_id(object(), "123", source="discord") == "456"
    assert await resolve_business_id(object(), "123", source="fluxer") == "123"
    with pytest.raises(ValueError):
        await resolve_business_id(object(), 999)


@pytest.mark.asyncio
async def test_publish_requires_discord_but_principal_remains_usable(monkeypatch):
    """原生用户可取得业务身份，但不能向 Discord 发布书单。"""
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    monkeypatch.setattr(
        "api.v1.routers.booklists._booklist_publish_base_url", "https://example.invalid"
    )
    principal = {"id": "123", "discord_user_id": None}
    assert current_identity_id(principal) == "123"
    with pytest.raises(HTTPException) as error:
        await publish_booklist(
            1,
            BooklistPublishRequest(thread_url="https://discord.com/channels/1/2"),
            principal,
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_preferences_reject_another_user(monkeypatch):
    """偏好接口不允许数字碰撞或任意其他用户的读写。"""
    from api.v1.routers.preferences import get_user_preferences

    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    with pytest.raises(HTTPException) as error:
        await get_user_preferences("200", {"id": "100"})
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_tag_owner_uses_canonical_identity(monkeypatch):
    """Discord ID 数字碰撞不能授予他人书单的标签管理权。"""
    from core.tag_access_service import TagAccessService
    from shared.tag_error import TagError

    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    service = TagAccessService({"main_guild_id": 1})
    service.roles = AsyncMock(return_value=set())
    target = SimpleNamespace(owner_id=123, fluxer_owner_id="456", is_public=True)
    with pytest.raises(TagError):
        await service.access(
            123, "booklist", target, manage=True, canonical_user_id="123"
        )
    assert await service.access(
        123, "booklist", target, manage=True, canonical_user_id="456"
    )


@pytest.mark.asyncio
async def test_unpublish_requires_linked_discord(monkeypatch):
    """取消 Discord 发布同样需要绑定，不能把统一 ID 当成 Discord 用户。"""
    from api.v1.routers.booklists import unpublish_booklist

    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    with pytest.raises(HTTPException) as error:
        await unpublish_booklist(1, {"id": "123", "discord_user_id": None})
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_background_publish_sync_defers_missing_discord_binding(monkeypatch):
    """解绑或映射未同步时，后台不会继续向 Discord 更新书单。"""
    from booklist.booklist_publish_service import BooklistPublishService

    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    monkeypatch.setattr(
        "identity.identity_repository.IdentityRepository.fluxer_to_discord",
        AsyncMock(return_value={}),
    )
    service = BooklistPublishService(object(), base_url="https://example.invalid")
    service.publish_repo.get_by_booklist = AsyncMock(
        return_value=SimpleNamespace(fluxer_user_id="200", discord_user_id=100)
    )
    service.publish_repo.upsert = AsyncMock()
    await service.sync_published_booklist(1)
    service.publish_repo.upsert.assert_not_awaited()
