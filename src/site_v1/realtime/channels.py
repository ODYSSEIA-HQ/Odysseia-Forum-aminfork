"""频道命名（前端 src/shared/realtime/centrifugo.ts）和订阅权限。

- personal:{userId}  本人：外层事件（提醒、未读、上传、资格、禁言）和私信的面板事件（带 target）
- site               全站：audience=all 的公告
- site_r18           全站：audience=r18 的公告；只在有效模式为 r18 时放进连接令牌
- work:{id}          作品页：work.updated / work.visibility.changed
- chat:work.{id}     作品交流区的面板事件
- chat:channel.{id}  公共聊天区的面板事件
personal、site、site_r18 是连接令牌里的服务端订阅；work、chat 由前端按需订阅，要订阅令牌。
"""

import re

from site_v1 import mode as modes
from site_v1.errors import forbidden, not_found

SITE = "site"
SITE_R18 = "site_r18"
MAX_CHANNEL = 200

_WORK = re.compile(r"work:([0-9]{1,19})")
_CHAT = re.compile(r"chat:(work|channel)\.([^\s]{1,128})")
_PERSONAL = re.compile(r"personal:(.{1,128})")


def personal(user_id: str) -> str:
    return f"personal:{user_id}"


def work(work_id) -> str:
    return f"work:{work_id}"


def chat(kind: str, target_id) -> str:
    return f"chat:{kind}.{target_id}"


def server_side_channels(ctx) -> list[str]:
    """连接令牌的 channels 声明：本人频道（登录时）、site、site_r18（有效模式 r18 时）。"""
    channels = []
    if ctx.viewer is not None:
        channels.append(personal(ctx.viewer.user_id))
    channels.append(SITE)
    if ctx.effective_mode == modes.R18:
        channels.append(SITE_R18)
    return channels


def is_tracked(channel: str) -> bool:
    """作品页、交流区的订阅：作品转为受限、下架时要把看不到的人踢出去。"""
    return channel.startswith("work:") or channel.startswith("chat:work.")


async def check_subscribe(session, ctx, channel: str) -> None:
    """订阅令牌的权限：看不到 404，不是自己的 / 模式不够 403（前端两者都当「看不到了」）。

    作品、交流区、公共聊天区复用 work_access 和 chat_messages.resolve，规则和 HTTP 接口一致。
    """
    if not channel or len(channel) > MAX_CHANNEL:
        raise not_found()
    if channel == SITE:
        return
    if channel == SITE_R18:
        if ctx.effective_mode != modes.R18:
            raise forbidden("只有 R18 模式能订阅")
        return
    m = _PERSONAL.fullmatch(channel)
    if m:
        if ctx.viewer is None or ctx.viewer.user_id != m.group(1):
            raise forbidden("只能订阅自己的频道")
        return
    m = _WORK.fullmatch(channel)
    if m:
        from site_v1.services.moderation_common import visible_work

        if await visible_work(session, ctx, m.group(1)) is None:
            raise not_found()
        return
    m = _CHAT.fullmatch(channel)
    if m:
        from site_v1.services.chat_messages import resolve

        await resolve(session, ctx, m.group(1), m.group(2))
        return
    raise not_found()
