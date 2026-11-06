"""契约测试钩子 /__test__（不属于协议），只在 SITE_TEST_HOOKS=1 时挂载，不能部署到生产。

- POST /__test__/users/{username}/restricted-access  {"state": "granted"|"revoked"|"none"}
  直接改账号的 R18 资格（模拟站务撤销/恢复）。有效模式每个请求现算，所以同一会话的下一个请求立即生效。
"""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from models.site_account import SiteRestrictedAccess
from models.user import User
from shared.time_utils import utc_now
from site_v1.deps import get_session_factory
from site_v1.services.accounts import ensure_account_rows

router = APIRouter(prefix="/__test__", tags=["测试钩子"], include_in_schema=False)


@router.post("/users/{username}/restricted-access")
async def set_restricted_access(username: str, body: dict = Body(...)):
    state = body.get("state")
    if state not in ("granted", "revoked", "none"):
        return JSONResponse(
            {"error": "state 只能是 granted、revoked、none"}, status_code=422
        )
    async with get_session_factory()() as session:
        user = (
            (
                await session.execute(
                    select(User).where(
                        User.username == username, User.merged_into_id.is_(None)
                    )
                )
            )
            .scalars()
            .first()
        )
        if user is None:
            return JSONResponse({"error": "no such user"}, status_code=404)
        await ensure_account_rows(session, user.id)
        row = await session.get(SiteRestrictedAccess, user.id, with_for_update=True)
        now = utc_now()
        row.state = state
        row.checked_at = now
        row.updated_at = now
        if state == "revoked":
            row.revoked_at = now
            row.revoked_by = "test-hook"
        elif state == "granted":
            row.source = row.source or "staff"
            row.revoked_at = None
            row.revoked_by = None
        await session.commit()
    return Response(status_code=204)
