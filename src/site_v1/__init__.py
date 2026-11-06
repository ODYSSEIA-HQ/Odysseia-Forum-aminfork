"""协议 /api/v1 层（共用接口协议 v0.4.1，给 odysseia-hq/web 前端用）。

不叫 site（和标准库冲突），也不放进 api/v1（那是旧的 Discord 接口）。
V1（/v1）、V2（/v2）和 fluxer 集成原样保留，这一层复用它们的服务。
说明见 docs/site_v1/README.md。
"""


def install(app) -> None:
    """把 /api/v1 挂到现有 FastAPI app：异常处理器、中间件、路由、测试钩子、启动钩子。"""
    import os

    from site_v1.config import get_config
    from site_v1.errors import install_exception_handlers
    from site_v1.middleware import SiteV1Middleware
    from site_v1.routers import router

    install_exception_handlers(app)
    app.add_middleware(SiteV1Middleware)
    app.include_router(router)
    if get_config().test_hooks:
        from site_v1.test_hooks import router as hooks

        app.include_router(hooks)

    @app.on_event("startup")
    async def site_v1_runtime():
        """独立用 uvicorn api.main:app 启动时（本地、契约测试）Redis 还没初始化，这里补上。"""
        from shared.redis_client import RedisManager

        if RedisManager._client is None:
            await RedisManager.init_redis(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            )
