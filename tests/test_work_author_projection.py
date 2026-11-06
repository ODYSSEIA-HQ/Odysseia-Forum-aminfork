"""Public Work author hydration is batched and never exposes account metadata."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from models.work import Work
from work.work_service import WorkService


@pytest.mark.asyncio
async def test_projection_batches_only_requested_public_author_fields():
    session = MagicMock()
    profile = SimpleNamespace(
        id="9007199254740993123",
        username="current-fluxer-user",
        display_name="Current display name",
        avatar_url="https://cdn.example.test/avatars/current.png",
    )
    empty = MagicMock()
    empty.scalars.return_value = []
    empty.all.return_value = []
    session.execute = AsyncMock(side_effect=[[profile], empty, empty, [], empty])
    works = [
        Work(id=1, title="Visible one", author_id=profile.id),
        Work(id=2, title="Visible two", author_id=profile.id),
        Work(id=3, title="Missing profile", author_id="missing"),
        Work(id=4, title="Unknown author", author_id=None),
    ]

    rows = await WorkService(session).project(works)

    assert (
        session.execute.await_count == 5
    )  # One author query, regardless of list size.
    assert rows[0]["author"] == rows[1]["author"] == vars(profile)
    assert rows[2]["author"] is None
    assert rows[2]["author_id"] == "missing"
    assert rows[3]["author"] is None
    query = session.execute.await_args_list[0].args[0]
    compiled = query.compile(dialect=postgresql.dialect())
    assert set(compiled.params["id_1"]) == {profile.id, "missing"}
    assert list(query.selected_columns.keys()) == [
        "id",
        "username",
        "display_name",
        "avatar_url",
    ]
    assert '"user".merged_into_id IS NULL' in str(compiled)
    assert "show_nsfw" not in str(compiled)


@pytest.mark.asyncio
async def test_empty_page_does_not_lookup_authors():
    session = MagicMock(execute=AsyncMock())
    assert await WorkService(session).project([]) == []
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_author_does_not_trigger_unbounded_user_lookup():
    empty = MagicMock()
    empty.scalars.return_value = []
    empty.all.return_value = []
    session = MagicMock(execute=AsyncMock(side_effect=[empty, empty, [], empty]))
    rows = await WorkService(session).project([Work(id=1, title="Unknown author")])
    assert rows[0]["author"] is None
    assert session.execute.await_count == 4
