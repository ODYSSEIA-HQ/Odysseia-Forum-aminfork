"""绑定事件必须原子更新 User 和本地绑定，失败后可恢复。"""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from models.user import User
from models.discord_identity import DiscordIdentity
from test_fluxer_auth_security import auth_service as auth_service, invalidation


@pytest_asyncio.fixture
async def projection_database(monkeypatch):
    """仅使用显式临时 PostgreSQL 和随机 schema。"""
    url = os.environ.get("FLUXER_MIGRATION_TEST_URL")
    if not url:
        pytest.skip("需要显式临时 PostgreSQL")
    url = url.replace("postgresql://", "postgresql+asyncpg://")
    schema = "test_binding_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.__table__.create(sync))
            await conn.run_sync(lambda sync: DiscordIdentity.__table__.create(sync))
        import shared.database

        monkeypatch.setattr(shared.database, "AsyncSessionFactory", factory)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_binding_projection_updates_profile_and_mapping_atomically(
    auth_service,  # noqa: F811
    projection_database,
):  # noqa: F811
    """绑定就绪前资料与映射均已提交，权威版本和原生资料一致。"""
    auth_service.replies[0] = {
        "ok": True,
        "data": {
            "profiles": [
                {
                    "user_id": "200",
                    "username": "native",
                    "display_name": "Native Name",
                    "avatar_url": None,
                }
            ]
        },
    }
    service = auth_service.service
    await service.invalidated(
        invalidation(
            "events.identity.v1.binding_changed",
            discord_user_id="100",
            user_id="200",
            version=3,
        )
    )
    assert service.ready
    async with projection_database() as session:
        user = await session.get(User, "200")
        binding = await session.get(DiscordIdentity, "100")
        assert user.username == "native"
        assert binding.user_id == user.id
        assert binding.version == 3


@pytest.mark.asyncio
async def test_conflicting_binding_rolls_back_profile_and_retains_event(
    auth_service,  # noqa: F811
    projection_database,
):  # noqa: F811
    """绑定唯一性冲突不能留下已更新但未匹配的 User 投影。"""
    async with projection_database() as session:
        session.add(User(id="200", username="original"))
        session.add(DiscordIdentity(discord_user_id="999", user_id="200", version=1))
        await session.commit()
    auth_service.replies[0] = {
        "ok": True,
        "data": {
            "profiles": [
                {
                    "user_id": "200",
                    "username": "must_rollback",
                    "display_name": None,
                    "avatar_url": None,
                }
            ]
        },
    }
    service = auth_service.service
    await service.invalidated(
        invalidation(
            "events.identity.v1.binding_changed",
            discord_user_id="100",
            user_id="200",
            version=3,
        )
    )
    await service.reset_task
    assert not service.ready
    assert service.pending_bindings["100"]["version"] == 3
    async with projection_database() as session:
        assert (await session.get(User, "200")).username == "original"
        assert await session.get(DiscordIdentity, "100") is None
