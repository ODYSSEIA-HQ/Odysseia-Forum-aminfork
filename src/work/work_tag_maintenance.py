"""标签申请到期维护，不依赖平台任务队列是否空闲。"""

import asyncio
import logging
from tag.tag_worker import TagWorker

logger = logging.getLogger(__name__)


class WorkTagMaintenance:
    """复用既有审核事务、期限及目标锁，避免另建审核事实。"""

    def __init__(self, sessions, config):
        """注入会话与正式权限配置，不保存请求身份。"""
        self.sessions = sessions
        self.worker = TagWorker(sessions, config)
        self.runner = None

    async def start(self):
        """启动独立周期，任务积压不会延迟七天自动审核。"""
        if self.runner is None:
            self.runner = asyncio.create_task(self.run())

    async def close(self):
        """取消循环，进行中的事务由上下文安全回滚。"""
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
            self.runner = None

    async def run(self):
        """每分钟处理有界批次，异常不会停止后续审核。"""
        while True:
            try:
                from identity.identity_projection_service import (
                    IdentityProjectionService,
                )

                async with self.sessions() as session:
                    await IdentityProjectionService(session).once()
                    await session.commit()
                await self.worker.expire()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("标签期限维护失败")
            await asyncio.sleep(60)
