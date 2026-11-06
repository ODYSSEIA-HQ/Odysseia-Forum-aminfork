"""/api/v1 路由汇总：每个领域一个文件，全部挂到 APIRouter(prefix="/api/v1")。"""

from fastapi import APIRouter

from site_v1.routers import (
    auth,
    chat,
    collections,
    friends,
    me,
    me_extra,
    notifications,
    work_extras,
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
    # 「我的」、提醒、公告规则、作品内、书单、好友（第二期 me 块）
    me_extra,
    notifications,
    work_extras,
    collections,
    friends,
):
    router.include_router(module.router)

__all__ = ["router"]
