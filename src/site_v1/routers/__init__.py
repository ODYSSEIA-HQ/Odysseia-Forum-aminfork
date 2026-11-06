"""/api/v1 路由汇总：每个领域一个文件，全部挂到 APIRouter(prefix="/api/v1")。"""

from fastapi import APIRouter

from site_v1.routers import (
    auth,
    channels,
    chat,
    home,
    chat_messages,
    collections,
    friends,
    me,
    me_extra,
    notifications,
    realtime,
    work_extras,
    moderation,
    reports,
    site,
    tag_pages,
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
    home,  # /boards/overview 要在 tags 的 /boards/{id} 之前
    tags,
    tag_pages,  # /tags/{id} 要在 tags 的 /tags/catalog、/tags/suggest 之后
    channels,
    chat,
    chat_messages,
    reports,
    moderation,
    events,
    fun,
    share,
    # 「我的」、提醒、公告规则、作品内、书单、好友（第二期 me 块）
    me_extra,
    notifications,
    work_extras,
    collections,
    friends,
    realtime,  # 实时推送令牌（Centrifugo）
):
    router.include_router(module.router)

__all__ = ["router"]
