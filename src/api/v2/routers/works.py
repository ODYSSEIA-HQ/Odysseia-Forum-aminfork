"""V2领域路由聚合，不包含业务SQL。"""

from fastapi import APIRouter
from shared.database import AsyncSessionFactory
from api.v2.routers.profiles import router as profiles_router
from api.v2.routers.collections import router as collections_router
from api.v2.routers.booklists import router as booklists_router
from api.v2.routers.tags import router as tags_router
from api.v2.routers.follows import router as follows_router
from api.v2.routers.authors import router as authors_router

__all__ = ["router", "AsyncSessionFactory"]
router = APIRouter()
router.include_router(profiles_router)
router.include_router(collections_router)
router.include_router(booklists_router)
router.include_router(tags_router)
router.include_router(follows_router)
router.include_router(authors_router)
