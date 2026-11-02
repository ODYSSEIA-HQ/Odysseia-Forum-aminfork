"""在独立 PostgreSQL schema 验证 Fluxer 标签生命周期。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from test_custom_tag_governance import setup_tags, create, attach
from models import Booklist, Thread, User, DiscordIdentity, Notification
from models.operation_log import OperationLog
from models.tag_proposal import TagProposal
from models.tag_vote import TagVote
from shared.time_utils import utc_now
from tag.tag_worker import TagWorker


@pytest.mark.asyncio
async def test_fluxer_tag_vote_and_timeout_lifecycle(setup_tags, monkeypatch):
    """验证绑定、投票、审核提醒和自动完成均使用统一身份。"""
    factory, config, call = setup_tags
    async with factory() as session, session.begin():
        for discord_id, user_id in [(1, "200"), (2, "100"), (3, "300"), (99, "900")]:
            session.add(User(id=user_id, username=f"user{user_id}"))
            session.add(
                DiscordIdentity(
                    discord_user_id=str(discord_id), user_id=user_id, version=1
                )
            )
        (await session.get(Thread, 100)).fluxer_author_id = "200"
        (await session.get(Booklist, 1)).fluxer_owner_id = "200"
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    tag = await create(call, "first")
    result = await attach(call, [tag["id"]], "booklist", 1)
    binding_id = result["tags"][0]["binding_id"]
    await call(
        "vote", 2, target_type="booklist", target_id=1, binding_id=binding_id, vote=1
    )
    async with factory() as session:
        vote = (await session.execute(select(TagVote))).scalar_one()
        assert vote.user_id == 2 and vote.fluxer_user_id == "100"
        logs = list((await session.execute(select(OperationLog))).scalars())
        assert any(
            row.type == "tag.vote" and row.fluxer_actor_id == "100" for row in logs
        )
    second = await create(call, "second")
    proposal = await call(
        "propose", 3, target_type="booklist", target_id=1, tag_id=second["id"]
    )
    worker = TagWorker(factory, config)

    async def failed_send(_notice):
        return False

    await worker.deliver(failed_send)
    async with factory() as session, session.begin():
        notice = (await session.execute(select(Notification))).scalar_one()
        assert notice.user_id == 1 and notice.fluxer_user_id == "200"
        row = await session.get(TagProposal, int(proposal["id"]))
        assert row.fluxer_applicant_id == "300" and row.fluxer_owner_id == "200"
        row.due_at = utc_now() - timedelta(seconds=1)
    await worker.expire()
    tags = await call("read", 1, target_type="booklist", target_id=1)
    assert {item["id"] for item in tags["tags"]} == {tag["id"], second["id"]}


@pytest.mark.asyncio
async def test_discord_ingestion_requires_mapping_and_preserves_system_zero(
    setup_tags, monkeypatch
):
    """完整与消息同步都拒绝未知作者，系统零值不被映射成账户。"""
    from core.thread_repository import ThreadRepository
    from dto.thread_message_snapshot import ThreadMessageSnapshot

    factory, _, _ = setup_tags
    async with factory() as session, session.begin():
        session.add(User(id="200", username="author"))
        session.add(DiscordIdentity(discord_user_id="1", user_id="200", version=1))
    monkeypatch.setenv("FORUM_AUTH_MODE", "fluxer")
    async with factory() as session, session.begin():
        await ThreadRepository(session).add_or_update_thread_with_tags(
            {"thread_id": 100, "author_id": 1}, []
        )
    async with factory() as session:
        thread = await session.get(Thread, 100)
        assert thread.author_id == 1 and thread.fluxer_author_id == "200"
    async with factory() as session:
        with pytest.raises(ValueError):
            await ThreadRepository(session).add_or_update_thread_with_tags(
                {"thread_id": 100, "author_id": 999}, []
            )
        await session.rollback()
    snapshot = ThreadMessageSnapshot(
        thread_id=100,
        author_id=999,
        created_at=utc_now(),
        reaction_count=0,
        first_message_excerpt="",
        thumbnail_urls=[],
    )
    async with factory() as session:
        with pytest.raises(ValueError):
            await ThreadRepository(session).update_thread_messages(snapshot)
        await session.rollback()
    async with factory() as session, session.begin():
        snapshot.author_id = 0
        await ThreadRepository(session).update_thread_messages(snapshot)
    async with factory() as session:
        thread = await session.get(Thread, 100)
        assert thread.author_id == 0 and thread.fluxer_author_id is None
