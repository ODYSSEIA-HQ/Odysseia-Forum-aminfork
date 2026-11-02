"""V2领域路由聚合，不包含业务SQL。"""

from fastapi import APIRouter
from shared.database import AsyncSessionFactory
from api.v2.routers.profiles import router as profiles_router
from api.v2.routers.search import router as search_router
from api.v2.routers.content import router as content_router
from api.v2.routers.collections import router as collections_router
from api.v2.routers.booklists import router as booklists_router
from api.v2.routers.tags import router as tags_router
from api.v2.routers.threads import router as threads_router
from api.v2.routers.announcements import router as announcements_router
from api.v2.routers.follows import router as follows_router
from api.v2.routers.notifications import router as notifications_router
from api.v2.routers.authors import router as authors_router
from api.v2.routers.moderation import router as moderation_router
from api.v2.routers.admin import router as admin_router

__all__ = ["router", "AsyncSessionFactory"]
router = APIRouter()
router.include_router(profiles_router)
router.include_router(search_router)
router.include_router(content_router)
router.include_router(collections_router)
router.include_router(booklists_router)
router.include_router(tags_router)
router.include_router(threads_router)
router.include_router(announcements_router)
router.include_router(follows_router)
router.include_router(notifications_router)
router.include_router(authors_router)
router.include_router(moderation_router)
router.include_router(admin_router)
