"""/api/v1 路由汇总：每个领域一个文件，全部挂到 APIRouter(prefix="/api/v1")。"""

from fastapi import APIRouter

from site_v1.routers import (
    auth,
    chat,
    me,
    moderation,
    reports,
    site,
    tags,
    uploads,
    users,
    versions,
    works,
)
from site_v1.routers import events, fun, share  # 活动、娱乐区、分享预览（events 块）

router = APIRouter(prefix="/api/v1")
for module in (
    site,
    auth,
    me,
    users,
    works,
    versions,
    uploads,
    tags,
    chat,
    reports,
    moderation,
    events,
    fun,
    share,
):
    router.include_router(module.router)

__all__ = ["router"]
