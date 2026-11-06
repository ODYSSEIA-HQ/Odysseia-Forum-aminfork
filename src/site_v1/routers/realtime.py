"""实时推送的令牌（前端 src/shared/realtime/centrifugo.ts）。

GET /realtime/token                     连接令牌 {url, token}：url 是 Centrifugo 的 WebSocket 地址；
                                        令牌的 channels 声明 = 服务端订阅（personal:{me}、site、
                                        有效模式 r18 时 site_r18）。未登录也能连（sub 为空串，只有 site）。
GET /realtime/subscribe-token?channel=  订阅令牌 {token}：work:{id}、chat:work.{id}、chat:channel.{id}
                                        按现有可见性规则校验；看不到 404，不是自己的 / 模式不够 403。
没配 CENTRIFUGO_TOKEN_SECRET / CENTRIFUGO_PUBLIC_URL 时两个接口都返回 503 temporarily_unavailable
（前端按随机退避重试，期间聊天面板轮询兜底）。
"""

import time

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from site_v1.deps import Ctx, get_session_factory
from site_v1.errors import unavailable
from site_v1.realtime import channels, jwt
from site_v1.realtime.config import realtime_config
from site_v1.realtime.events import track_subscriber

router = APIRouter(tags=["实时推送"])

NO_STORE = {"Cache-Control": "no-store"}


def _config():
    config = realtime_config()
    if not config.tokens_enabled:
        raise unavailable("实时推送没有开启")
    return config


@router.get("/realtime/token", summary="Centrifugo 连接令牌（含服务端订阅）和连接地址")
async def connection_token(ctx: Ctx):
    config = _config()
    now = int(time.time())
    claims = {
        "sub": ctx.viewer.user_id if ctx.viewer else "",
        "iat": now,
        "exp": now + config.token_ttl,
        "channels": channels.server_side_channels(ctx),
    }
    return JSONResponse(
        {"url": config.public_url, "token": jwt.encode(claims, config.token_secret)},
        headers=NO_STORE,
    )


@router.get(
    "/realtime/subscribe-token",
    summary="Centrifugo 订阅令牌（作品页、交流区、公共聊天区）",
)
async def subscribe_token(ctx: Ctx, channel: str = Query("")):
    config = _config()
    async with get_session_factory()() as session:
        await channels.check_subscribe(session, ctx, channel)
    now = int(time.time())
    sub = ctx.viewer.user_id if ctx.viewer else ""
    claims = {
        "sub": sub,
        "channel": channel,
        "iat": now,
        "exp": now + config.sub_token_ttl,
    }
    if sub:
        await track_subscriber(channel, sub)
    return JSONResponse(
        {"token": jwt.encode(claims, config.token_secret)}, headers=NO_STORE
    )
