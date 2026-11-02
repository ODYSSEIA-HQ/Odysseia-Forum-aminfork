"""JetStream 持久内容和认证事件消费。"""

import asyncio
import json
import logging
import os
import time
from types import SimpleNamespace
from sqlalchemy.dialects.postgresql import insert
from models.work_event import WorkEvent
from identity.auth_runtime import get_auth_service
from shared.database import AsyncSessionFactory
from shared.time_utils import utc_now
from work.work_event_service import WorkEventService

logger = logging.getLogger(__name__)


class WorkEventConsumer:
    """只有 PG 提交后 ACK，断线与进程重启均可安全重送。"""

    def __init__(self, session_factory=AsyncSessionFactory, auth=None):
        """消费者由部署管理员预建，不授予 Forum 建流和任意订阅权限。"""
        self.sessions = session_factory
        self.auth = auth
        self.runners = []

    async def start(self):
        """分别启动认证和内容持久拉取消费者。"""
        if not self.runners:
            self.runners = [
                asyncio.create_task(self.run(kind)) for kind in ("auth", "content")
            ]

    async def close(self):
        """停止拉取，未 ACK 事件留在 JetStream。"""
        for runner in self.runners:
            runner.cancel()
        if self.runners:
            await asyncio.gather(*self.runners, return_exceptions=True)
        self.runners = []

    async def run(self, kind):
        """重连绑定同一 durable，启动认证服务会轮换全部缓存代次。"""
        from nats.errors import TimeoutError

        while True:
            subscription = None
            try:
                auth = self.auth or get_auth_service()
                await auth.start()
                js = auth.connection.jetstream()
                stream = os.environ.get(
                    "INTEGRATION_" + kind.upper() + "_STREAM",
                    "ODYSSEIA_AUTH" if kind == "auth" else "ODYSSEIA_CONTENT",
                )
                durable = os.environ.get(
                    "INTEGRATION_" + kind.upper() + "_CONSUMER",
                    "forum-auth" if kind == "auth" else "forum-index",
                )
                continuity = None
                checked = time.monotonic()
                if kind == "auth":
                    from identity.auth_event_continuity import AuthEventContinuity

                    continuity = AuthEventContinuity(auth.redis)
                    await continuity.check(js, stream, durable, startup=True)
                subscription = await js.pull_subscribe_bind(
                    durable=durable, stream=stream
                )
                while auth.ready:
                    if continuity and time.monotonic() - checked >= 30:
                        await continuity.check(js, stream, durable)
                        checked = time.monotonic()
                    try:
                        messages = await subscription.fetch(batch=32, timeout=1)
                    except TimeoutError:
                        continue
                    for message in messages:
                        try:
                            await self.process(kind, message, auth)
                            await message.ack_sync(timeout=3)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("持久事件处理失败 kind=%s", kind)
                            await message.nak(delay=5)
            except asyncio.CancelledError:
                raise
            except Exception:
                if kind == "auth":
                    auth = self.auth or get_auth_service()
                    await auth.request_reset()
                logger.exception("持久事件消费者重连 kind=%s", kind)
                await asyncio.sleep(2)
            finally:
                if subscription:
                    await subscription.unsubscribe()

    async def process(self, kind, message, auth):
        """认证事件先失效缓存和更新绑定，再持久记录完成。"""
        from identity.runtime import request_identity_mode

        marker = request_identity_mode.set("fluxer")
        try:
            event = json.loads(message.data)
            if not isinstance(event.get("event_id"), str) or not event["event_id"]:
                raise ValueError("事件缺少永久幂等编号")
            async with self.sessions() as session:
                if kind == "content":
                    if message.subject.endswith(".deleted"):
                        event["deleted"] = True
                    from api.v2.config import get_work_config
                    from identity.identity_profile_service import IdentityProfileService

                    author = event.get("snapshot", {}).get("author_id") or event.get(
                        "author_id"
                    )
                    if author and not event.get("deleted"):
                        await IdentityProfileService(session, auth.rpc).ensure([author])
                    await WorkEventService(
                        session, get_work_config().get("work_forums", {})
                    ).apply(event)
                else:
                    existing = await session.get(WorkEvent, event["event_id"])
                    if existing is not None:
                        return
                    accepted = {
                        "events.auth.v1.session_invalidated",
                        "events.auth.v1.user_invalidated",
                        "events.identity.v1.binding_changed",
                    }
                    if message.subject not in accepted:
                        raise ValueError("未知认证事件类型")
                    await auth.invalidated(
                        SimpleNamespace(subject=message.subject, data=message.data)
                    )
                    if not auth.ready:
                        raise RuntimeError("认证事件未完成，禁止 ACK")
                    await session.execute(
                        insert(WorkEvent)
                        .values(
                            event_id=event["event_id"],
                            payload=event,
                            received_at=utc_now(),
                        )
                        .on_conflict_do_nothing()
                    )
                await session.commit()
        finally:
            request_identity_mode.reset(marker)
