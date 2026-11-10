"""调 Centrifugo 的 HTTP server API：publish / disconnect / unsubscribe。

- 只在事务提交之后调用（聊天在 commit 之后；ORM 变化由 hooks.py 在 after_commit 里排队）；
- 没配 CENTRIFUGO_API_URL / CENTRIFUGO_API_KEY 时全部是空操作；
- 失败（连不上、超时、Centrifugo 返回 error）只记日志，不抛给调用方，不影响请求；
- spawn() 把发布放进后台任务，请求不等 Centrifugo；测试用 drain() 等它们跑完。

Centrifugo v5 / v6 的 HTTP API：POST {api_url}/api/{method}，头 X-API-Key，JSON 请求体，
响应 {"result": {...}} 或 {"error": {"code", "message"}}。
"""

import asyncio
import logging

import httpx

from site_v1.realtime.config import realtime_config

logger = logging.getLogger(__name__)

# 权限变化时断开连接用的代码（前端 centrifugo.ts：3014 = 丢掉旧令牌重新取）
DISCONNECT_ACCESS_CHANGED = 3014

_transport: httpx.AsyncBaseTransport | None = None
_tasks: set[asyncio.Task] = set()


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试换成 httpx.MockTransport / ASGITransport（假 Centrifugo）。"""
    global _transport
    _transport = transport


async def call(method: str, params: dict) -> dict | None:
    """调一个 server API 方法；成功返回 result（可能是空对象），失败或没配置返回 None。"""
    config = realtime_config()
    if not config.publish_enabled:
        return None
    try:
        async with httpx.AsyncClient(
            transport=_transport, timeout=config.publish_timeout
        ) as client:
            res = await client.post(
                f"{config.api_url}/api/{method}",
                json=params,
                headers={"X-API-Key": config.api_key},
            )
        if res.status_code != 200:
            logger.warning(
                "Centrifugo %s 返回 HTTP %s：%s",
                method,
                res.status_code,
                res.text[:200],
            )
            return None
        body = res.json() if res.content else {}
        if isinstance(body, dict) and body.get("error"):
            logger.warning("Centrifugo %s 出错：%s", method, body["error"])
            return None
        return (body.get("result") if isinstance(body, dict) else None) or {}
    except Exception as exc:  # noqa: BLE001 — 推送失败不影响请求
        logger.warning("Centrifugo %s 调用失败：%s", method, exc)
        return None


async def publish(channel: str, data: dict) -> bool:
    return await call("publish", {"channel": channel, "data": data}) is not None


async def disconnect(
    user_id: str, code: int = DISCONNECT_ACCESS_CHANGED, reason: str = "access changed"
) -> bool:
    if not user_id:
        return False
    params = {"user": user_id, "disconnect": {"code": code, "reason": reason}}
    return await call("disconnect", params) is not None


async def unsubscribe(user_id: str, channel: str) -> bool:
    if not user_id:
        return False
    return await call("unsubscribe", {"user": user_id, "channel": channel}) is not None


async def _guard(coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        logger.exception("实时推送后台任务失败")


def spawn(coro) -> None:
    """后台跑一个推送协程；没有运行中的事件循环（同步脚本）时直接丢掉。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(_guard(coro))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def publish_later(channel: str, data: dict) -> None:
    if not realtime_config().publish_enabled:
        return
    spawn(publish(channel, data))


async def drain() -> None:
    """等当前事件循环里排着的推送任务全部跑完（测试、关停时用）。"""
    loop = asyncio.get_running_loop()
    while True:
        pending = [t for t in _tasks if t.get_loop() is loop and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
