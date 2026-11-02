"""真实 PostgreSQL 验证统一作品复用既有 Banner 工作流。"""

import os
from datetime import timedelta
import pytest
import pytest_asyncio
import test_work_v2_postgres as database_tests
from sqlalchemy import select, update

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from models import BannerApplication, BannerCarousel, BannerWaitlist, User, Thread
from models.work import Work
from banner.banner_service import BannerService
from work.work_banner_service import WorkBannerService
from shared.time_utils import utc_now
from fastapi import HTTPException

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_WORK_DB_URL"), reason="需要显式隔离 PostgreSQL"
)


@pytest_asyncio.fixture
async def factory(monkeypatch):
    """复用独立根事务，每个测试的业务提交均最终回滚。"""
    async for sessions in database_tests.factory.__wrapped__(monkeypatch):
        yield sessions


def command(owner="u1", *, admin=False, values=None):
    """构造已经认证的领域主体，不携带原始 Bearer。"""
    actor = WorkActorDTO(
        id=owner,
        discord_user_id="",
        is_admin=admin,
        show_nsfw=True,
        can_view_abyss=True,
        forums={"normal": "200"},
    )
    return WorkCommandQo(actor=actor, values=values or {})


async def create_work(factory, owner="u1", title="原生作品"):
    """直接持久化原生作品，不借用或伪造 Discord 编号。"""
    async with factory() as session:
        if await session.get(User, owner) is None:
            session.add(User(id=owner, username=owner))
        row = Work(
            title=title,
            author_id=owner,
            source_platform="fluxer",
            source_guild_id="999",
            source_channel_id="200",
            images=["https://image.test/native.png"],
        )
        session.add(row)
        await session.flush()
        external_id = 90000000 + row.id
        row.source_thread_id = str(external_id)
        session.add(
            Thread(
                thread_id=external_id,
                platform="fluxer",
                work_id=row.id,
                guild_id=999,
                channel_id=200,
                title=title,
                fluxer_author_id=owner,
                thumbnail_urls=["https://image.test/native.png"],
            )
        )
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_native_work_application_review_and_display(factory):
    """原生申请不写假 Discord ID，审核直接产生三天轮播。"""
    work_id = await create_work(factory)
    service = WorkBannerService(factory)
    application = await service.apply(work_id, command())
    assert application["thread_id"] is None
    assert application["work_id"] == str(work_id)
    duplicate = await service.apply(work_id, command())
    assert duplicate["id"] == application["id"]
    approved = await service.review(
        int(application["id"]), command("u2", admin=True, values={"approve": True})
    )
    assert approved["status"] == "approved"
    assert approved["placement"] == "carousel"
    async with factory() as session:
        row = (await session.execute(select(BannerCarousel))).scalars().one()
        assert row.thread_id is None and row.work_id == work_id
        assert row.fluxer_owner_id == "u1"
        assert row.fluxer_applicant_id == "u1"
        assert row.end_time - row.start_time == timedelta(days=3)
        audit = await session.get(BannerApplication, int(application["id"]))
        assert audit.fluxer_reviewer_id == "u2"
    projected = await service.banner(command())
    assert projected["total"] == 1
    assert projected["items"][0]["work"]["id"] == str(work_id)
    assert (await service.applications(command(), 0, 20))["total"] == 1
    with pytest.raises(HTTPException) as repeat:
        await service.review(
            int(application["id"]), command("u2", admin=True, values={"approve": True})
        )
    assert repeat.value.status_code == 409


@pytest.mark.asyncio
async def test_global_capacity_fifo_and_native_cleanup(factory):
    """全局前三展示，第四候补，过期后按原 FIFO 晋升且保留业务归属。"""
    service = WorkBannerService(factory)
    ids = []
    for index in range(4):
        owner = f"u{index + 1}"
        work_id = await create_work(factory, owner)
        app = await service.apply(work_id, command(owner))
        result = await service.review(
            int(app["id"]), command("u2", admin=True, values={"approve": True})
        )
        assert result["placement"] == ("carousel" if index < 3 else "waitlist")
        ids.append(int(app["id"]))
    async with factory() as session:
        waiting = (await session.execute(select(BannerWaitlist))).scalars().one()
        assert waiting.application_id == ids[-1]
        await session.execute(
            update(BannerCarousel)
            .where(BannerCarousel.application_id == ids[0])
            .values(end_time=utc_now() - timedelta(seconds=1))
        )
        await session.commit()
        assert await BannerService(session).cleanup_expired_banners() == 1
        active = (await session.execute(select(BannerCarousel))).scalars().all()
        assert len(active) == 3
        promoted = next(row for row in active if row.application_id == ids[-1])
        assert promoted.fluxer_owner_id == promoted.fluxer_applicant_id == "u4"
        assert (await session.execute(select(BannerWaitlist))).scalars().all() == []


@pytest.mark.asyncio
async def test_channel_capacity_is_eight_and_wire_has_no_fake_ids(factory):
    """频道轮播容纳八条，原协议排除原生作品但真实容量包含它们。"""
    service = WorkBannerService(factory)
    for index in range(9):
        owner = f"u{index + 1}"
        app = await service.apply(
            await create_work(factory, owner),
            command(owner, values={"target_scope": "200"}),
        )
        result = await service.review(
            int(app["id"]), command("u2", admin=True, values={"approve": True})
        )
        assert result["placement"] == ("carousel" if index < 8 else "waitlist")
    async with factory() as session:
        banners = await BannerService(session).get_active_banners(channel_ids=[200])
        assert len(banners) == 8
        assert all(row.thread_id is None and row.target_type == 3 for row in banners)
        assert await BannerService(session).carousel_repo.get_count(200) == 8


@pytest.mark.asyncio
async def test_permissions_limits_and_rejection_keep_actual_audit(factory):
    """作者、管理、范围和占位限制均在领域层验证。"""
    work_id = await create_work(factory)
    service = WorkBannerService(factory)
    with pytest.raises(HTTPException) as other:
        await service.apply(work_id, command("u2"))
    assert other.value.status_code == 403
    with pytest.raises(HTTPException) as scope:
        await service.apply(work_id, command(values={"target_scope": "201"}))
    assert scope.value.status_code == 422
    app = await service.apply(work_id, command())
    with pytest.raises(HTTPException) as member:
        await service.review(int(app["id"]), command(values={"approve": True}))
    assert member.value.status_code == 403
    rejected = await service.review(
        int(app["id"]),
        command("u2", admin=True, values={"approve": False, "reason": "图片不适合"}),
    )
    assert rejected["reject_reason"] == "图片不适合"
    assert rejected["reviewer_id"] == "u2"
    assert rejected["placement"] is None
    replacement = await service.apply(work_id, command())
    await service.review(
        int(replacement["id"]), command("u2", admin=True, values={"approve": True})
    )
    with pytest.raises(HTTPException) as occupied:
        await service.apply(await create_work(factory), command())
    assert occupied.value.status_code == 409
    with pytest.raises(HTTPException) as admin_list:
        await service.applications(command(), 0, 20, admin=True)
    assert admin_list.value.status_code == 403


@pytest.mark.asyncio
async def test_banner_http_authorization_and_real_queue(factory, monkeypatch):
    """真实 HTTP 路由验证作者、管理以及审核后落入原队列。"""
    import httpx
    from fastapi import FastAPI
    from api.v2.routers import banner as router
    from api.v2.dependencies import current_member

    work_id = await create_work(factory)
    principal = dict(command().actor)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[current_member] = lambda: principal
    monkeypatch.setattr(router, "get_session_factory", lambda: factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        scopes = await client.get(f"/v2/works/{work_id}/banner-scopes")
        assert scopes.status_code == 200
        assert scopes.json()["items"][0]["id"] == "global"
        response = await client.post(
            f"/v2/works/{work_id}/banner-applications", json={}
        )
        assert response.status_code == 201
        identifier = response.json()["id"]
        assert (await client.get("/v2/me/banner-applications")).json()["total"] == 1
        assert (await client.get("/v2/admin/banner-applications")).status_code == 403
        principal.update(id="u2", is_admin=True)
        response = await client.patch(
            f"/v2/admin/banner-applications/{identifier}", json={"approve": True}
        )
        assert response.status_code == 200
        assert response.json()["placement"] == "carousel"
        listing = (await client.get("/v2/admin/banner-applications")).json()
        assert listing["total"] == len(listing["items"]) == 1
        assert listing["items"][0]["placement"] == "carousel"


@pytest.mark.asyncio
async def test_pending_starter_cannot_enter_banner_workflow(factory):
    """主帖未同步的作品不产生可批准申请或推荐展示。"""
    async with factory() as session:
        row = Work(
            title="主帖同步中",
            author_id="u1",
            source_platform="fluxer",
            images=["https://image.test/pending.png"],
        )
        session.add(row)
        await session.commit()
        work_id = row.id
    with pytest.raises(HTTPException) as pending:
        await WorkBannerService(factory).apply(work_id, command())
    assert pending.value.status_code == 409


@pytest.mark.asyncio
async def test_discord_read_failure_does_not_hide_banner_or_block_application(factory):
    """临时读取失败计数既不隐藏旧 Banner，也不阻止正常作者申请。"""
    from unittest.mock import MagicMock
    from core.thread_repository import ThreadRepository
    from core.banner_thread_visibility_service import BannerThreadVisibilityService

    await database_tests.seed_thread(factory, external=900510)
    await database_tests.seed_thread(factory, external=900510, platform="fluxer")
    async with factory() as session:
        for _ in range(6):
            await ThreadRepository(session).increment_not_found_count(900510)
        visible = await BannerThreadVisibilityService(
            session, MagicMock()
        ).get_visible_thread_guilds([900510], None, {})
        assert visible == {900510: 999}
        request = await BannerService(session).validate_application_request(
            900510, 999, 101, None, "global"
        )
        assert request.success


@pytest.mark.asyncio
async def test_managed_merge_moves_only_banner_business_owner(factory):
    """管理合并迁移业务归属，保留实际申请人和审核人历史。"""
    from identity.identity_merge_service import IdentityMergeService

    work_id = await create_work(factory)
    service = WorkBannerService(factory)
    application = await service.apply(work_id, command())
    await service.review(
        int(application["id"]), command("u2", admin=True, values={"approve": True})
    )
    async with factory() as session:
        await IdentityMergeService(session).merge(
            "u1", "u2", "banner_merge_operation_0001", "u2"
        )
        await session.commit()
        row = await session.get(BannerApplication, int(application["id"]))
        carousel = (await session.execute(select(BannerCarousel))).scalars().one()
        assert row.fluxer_owner_id == carousel.fluxer_owner_id == "u2"
        assert row.fluxer_applicant_id == carousel.fluxer_applicant_id == "u1"
        assert row.fluxer_reviewer_id == "u2"
    assert (await service.applications(command("u2"), 0, 20))["total"] == 1
    assert (await service.applications(command("u1"), 0, 20))["total"] == 0
