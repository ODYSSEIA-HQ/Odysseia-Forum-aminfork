"""/api/v1 通用层的纯函数：有效模式、分级映射、分页游标、错误映射、时间格式。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from site_v1 import mode
from site_v1.dto import camelize, iso_utc
from site_v1.errors import SiteError, from_http_exception
from site_v1.paging import (
    MAX_LIMIT,
    PageRequest,
    clamp_limit,
    encode_cursor,
    page_request,
    page_response,
)


@pytest.mark.parametrize(
    "authenticated,content_mode,state,expected",
    [
        (False, "r18", "granted", "general"),
        (True, "general", "granted", "general"),
        (True, "r18", "none", "general"),
        (True, "r18", "revoked", "general"),
        (True, "r18", "granted", "r18"),
    ],
)
def test_effective_mode_requires_all_three(
    authenticated, content_mode, state, expected
):
    """有效模式 r18 要同时满足：已登录、contentMode=r18、资格 granted。"""
    assert mode.effective_mode(authenticated, content_mode, state) == expected


def test_rating_mapping_and_visibility():
    """深渊 → r18g，NSFW → r18；r18g 只在 r18 模式且 showR18G 时可见。"""
    assert mode.work_rating(False, False) == "general"
    assert mode.work_rating(True, False) == "r18"
    assert mode.work_rating(False, True) == "r18g"
    assert mode.work_rating(True, True) == "r18g"
    assert mode.rating_flags("r18") == (True, False)
    assert mode.visible_ratings("general", True) == {"general"}
    assert mode.visible_ratings("r18", False) == {"general", "r18"}
    assert mode.visible_ratings("r18", True) == {"general", "r18", "r18g"}
    # rating 参数只能收窄
    assert mode.narrow_ratings(["r18"], "general", True) == set()
    assert mode.narrow_ratings(["r18", "general"], "r18", False) == {"r18", "general"}
    # 破限 tag 让全年龄作品也变 restricted
    assert mode.work_visibility("general", True) == "restricted"
    assert not mode.can_view("general", False, "general", "restricted")


def test_limit_is_clamped_not_rejected():
    """limit 超过 100 按 100，坏值按默认 20。"""
    assert clamp_limit("1000") == MAX_LIMIT
    assert clamp_limit(0) == 20
    assert clamp_limit("abc") == 20
    assert clamp_limit(7) == 7


def test_offset_and_keyset_cursors():
    """offset 游标和 keyset 游标都能往返；最后一页 nextCursor 为 null。"""
    page = page_request(None, 2)
    first = page_response([1, 2, 3], page, lambda x: x)
    assert first["items"] == [1, 2] and first["nextCursor"]
    second = page_request(first["nextCursor"], 2)
    assert second.offset == 2
    last = page_response([3], second, lambda x: x)
    assert last == {"items": [3], "nextCursor": None}

    keyset = page_response(
        ["a", "b", "c"], PageRequest(limit=2), str, key=lambda r: [r, 1]
    )
    assert page_request(keyset["nextCursor"], 2).after == ["b", 1]


def test_bad_cursor_is_validation_failed():
    with pytest.raises(SiteError) as caught:
        page_request("!!!not-base64", 20)
    assert caught.value.status == 422 and "cursor" in caught.value.details["fields"]
    with pytest.raises(SiteError):
        page_request(encode_cursor({"o": -1}), 20)


def test_http_exception_mapping():
    """V2 服务的 HTTPException 映射成协议错误码；504 统一成 503 并带 Retry-After。"""
    timeout = from_http_exception(HTTPException(504, "搜索超时"))
    assert (timeout.status, timeout.code) == (503, "temporarily_unavailable")
    assert timeout.retry_after and timeout.retry_after > 0
    assert from_http_exception(HTTPException(401, "x")).code == "unauthenticated"
    assert from_http_exception(HTTPException(403, "x")).code == "forbidden"
    assert from_http_exception(HTTPException(404, "x")).code == "not_found"
    assert from_http_exception(HTTPException(409, "x")).code == "conflict"
    invalid = from_http_exception(HTTPException(422, "不能关注自己"))
    assert invalid.code == "validation_failed" and invalid.details == {"fields": {}}
    assert from_http_exception(HTTPException(400, "x")).status == 422


def test_iso_utc_and_camelize():
    naive = datetime(2026, 10, 16, 13, 30, 30, 123456)
    assert iso_utc(naive) == "2026-10-16T13:30:30.123Z"
    aware = datetime(2026, 10, 16, 21, 30, 30, tzinfo=timezone(timedelta(hours=8)))
    assert iso_utc(aware) == "2026-10-16T13:30:30.000Z"
    assert iso_utc(None) is None
    assert camelize({"content_mode": "r18", "nested_list": [{"show_r18g": True}]}) == {
        "contentMode": "r18",
        "nestedList": [{"showR18G": True}],
    }


@pytest.mark.asyncio
async def test_set_classification_uses_v2_move_task_when_fluxer_threads(monkeypatch):
    """改分级的统一入口：真实环境、作品有 Fluxer 帖子时走 V2 WorkService.update（排分类移动任务），
    审核改分级（moderation.set_work_rating）也调这里。"""
    from types import SimpleNamespace

    from site_v1.context import Prefs, RestrictedAccess, Viewer
    from site_v1.services import works

    calls = []

    class FakeWorkService:
        def __init__(self, session):
            pass

        async def update(self, work, changes, principal, forums):
            calls.append((changes, principal["is_admin"]))

    async def has_threads(session, work_id):
        return True

    import work.work_service as v2

    monkeypatch.setattr(v2, "WorkService", FakeWorkService)
    monkeypatch.setattr(works, "has_fluxer_threads", has_threads)
    monkeypatch.setattr(works, "get_config", lambda: SimpleNamespace(fluxer_fake=False))
    viewer = Viewer(
        user_id="9",
        username="staff",
        display_name=None,
        avatar_url=None,
        discord_user_id=None,
        email=None,
        created_at=datetime.now(timezone.utc),
        newcomer_until=None,
        quiz_passed=True,
        permissions=frozenset(),
        prefs=Prefs(),
        access=RestrictedAccess(),
    )
    work_row = SimpleNamespace(id=1, is_nsfw=False, is_abyss=False, classification_revision=0)
    await works.set_classification(
        None, work_row, "r18g", actor=viewer, mode="r18", is_admin=True
    )
    assert calls == [({"is_nsfw": True, "is_abyss": True}, True)]
    # 没有操作者（或 fake 模式）时直接改字段
    await works.set_classification(
        None, work_row, "r18", actor=None, mode="r18", is_admin=True
    )
    assert (work_row.is_nsfw, work_row.is_abyss, work_row.classification_revision) == (
        True,
        False,
        1,
    )
