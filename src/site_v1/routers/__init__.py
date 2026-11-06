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
):
    router.include_router(module.router)

__all__ = ["router"]
