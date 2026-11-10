"""/api/v1 路由用的依赖：数据库会话工厂、Redis、请求上下文。

后面的领域路由这样写：

    from site_v1.deps import Ctx, CurrentUser

    @router.get("/works/{id}")
    async def get_work(id: str, ctx: Ctx):           # 公开接口：ctx.viewer 可能是 None
        ...

    @router.post("/works")
    async def create_work(body: ..., viewer: CurrentUser, ctx: Ctx):   # 需要登录：未登录 401
        ...
"""

from typing import Annotated

from fastapi import Depends, Request

from site_v1.context import SiteContext, Viewer
from site_v1.errors import unavailable

_session_factory = None


def get_session_factory():
    """数据库会话工厂；测试用 set_session_factory 换成测试库。"""
    if _session_factory is not None:
        return _session_factory
    from shared.database import AsyncSessionFactory

    return AsyncSessionFactory


def set_session_factory(factory) -> None:
    global _session_factory
    _session_factory = factory


def get_redis():
    from shared.redis_client import RedisManager

    return RedisManager.get_client()


def get_ctx(request: Request) -> SiteContext:
    ctx = getattr(request.state, "site_ctx", None)
    if ctx is None:
        # 中间件没跑（例如挂载顺序错了）时不放行
        raise unavailable("会话服务未初始化")
    return ctx


def require_user(request: Request) -> Viewer:
    return get_ctx(request).require_user()


Ctx = Annotated[SiteContext, Depends(get_ctx)]
CurrentUser = Annotated[Viewer, Depends(require_user)]
