"""GET /healthz：网关显示各核心状态用（网关与核心接入约定第 4 节）。

不在 /api/v1 下，不需要身份头。检查项同 /v1/health 的数据库、Redis 两项
（/v1/health 还要求 V1 频道缓存就绪，核心用不到）；都正常 200，否则 503。
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from site_v1.config import get_config

logger = logging.getLogger(__name__)
router = APIRouter(tags=["系统"])


@router.get("/healthz", summary="核心健康检查", include_in_schema=False)
async def healthz():
    from site_v1.deps import get_redis, get_session_factory

    checks = {}
    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        checks["database"] = "unavailable"
        logger.warning("healthz：数据库不可达", exc_info=True)
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception:  # noqa: BLE001
        checks["redis"] = "unavailable"
        logger.warning("healthz：Redis 不可达", exc_info=True)
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "core": "weiyu",
            "authMode": get_config().auth_mode,
            "checks": checks,
        },
        status_code=200 if healthy else 503,
        headers={"Cache-Control": "no-store"},
    )
