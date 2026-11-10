"""Fluxer 原生 HTTP 接口（Discord 兼容形状）的服务端客户端：聊天消息代转用。

前端不再直连 Fluxer（令牌不进浏览器），由 Forum 拿会话里存的 Fluxer 令牌代调：

- 基地址 FLUXER_API_URL（同微雨前端 auth config 里的 fluxer_api_url，例如 https://api.fluxer.app/v1）；
- 请求头 Authorization: <用户令牌>（不带 Bearer，同 nativeApi.ts）、X-Fluxer-Features: channel_threads；
- 删除别人的消息（作者、卡贴管理员、站务删言）用 FLUXER_BOT_TOKEN（Authorization: Bot <token>），
  权限由 Forum 判断；没配时退回用户令牌（Fluxer 侧要有 MANAGE_MESSAGES 才行）。

用到的接口见 docs/site_v1/fluxer-rpc.md 第 7 节。
"""

import os
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from site_v1.fluxer.port import (
    FluxerForbidden,
    FluxerMessage,
    FluxerNotFound,
    FluxerUnauthorized,
    FluxerUnavailable,
)

_client: httpx.AsyncClient | None = None


def api_base() -> str | None:
    base = (os.getenv("FLUXER_API_URL") or "").strip().rstrip("/")
    return base or None


def bot_token() -> str | None:
    token = (os.getenv("FLUXER_BOT_TOKEN") or "").strip()
    return token or None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        timeout = float(os.getenv("FLUXER_API_TIMEOUT", "10") or 10)
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """测试用：换成 MockTransport 的客户端。"""
    global _client
    _client = client


def _q(value: str) -> str:
    return quote(str(value), safe="")


async def request(
    method: str,
    path: str,
    *,
    token: str | None,
    bot: bool = False,
    params: dict | None = None,
    json: dict | None = None,
):
    base = api_base()
    if not base:
        raise FluxerUnavailable("没有配置 FLUXER_API_URL")
    if bot and bot_token():
        auth = f"Bot {bot_token()}"
    elif token:
        auth = token
    else:
        raise FluxerUnauthorized("缺少 Fluxer 令牌")
    headers = {"Authorization": auth, "X-Fluxer-Features": "channel_threads"}
    try:
        res = await _http().request(
            method,
            base + path,
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=json,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise FluxerUnavailable(f"Fluxer 请求失败：{exc.__class__.__name__}") from exc
    if res.status_code == 401:
        raise FluxerUnauthorized("Fluxer 令牌无效")
    if res.status_code == 404:
        raise FluxerNotFound(path)
    if res.status_code == 403:
        # Discord 兼容：看不到频道是 403 Missing Access(50001)，按不存在处理；其余是没权限
        code = _error_code(res)
        if code in (50001, 10003):
            raise FluxerNotFound(path)
        raise FluxerForbidden(_error_message(res))
    if res.status_code >= 400:
        if res.status_code == 400 and _error_code(res) in (10008, 10003):
            raise FluxerNotFound(path)
        raise FluxerUnavailable(f"Fluxer 返回 {res.status_code}")
    if res.status_code == 204 or not res.content:
        return None
    try:
        return res.json()
    except ValueError as exc:
        raise FluxerUnavailable("Fluxer 返回了无效 JSON") from exc


def _error_code(res) -> int | None:
    try:
        return int(res.json().get("code"))
    except Exception:
        return None


def _error_message(res) -> str:
    try:
        return str(res.json().get("message") or "Fluxer 拒绝了这个操作")
    except Exception:
        return "Fluxer 拒绝了这个操作"


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (
        dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{dt.microsecond // 1000:03d}Z"
    )


def emoji_key(emoji: dict | None) -> str:
    """原生 reaction 的 emoji → 协议里的表情字符串（unicode 原样，自定义表情 name:id）。"""
    emoji = emoji or {}
    if emoji.get("id"):
        return f"{emoji.get('name') or ''}:{emoji['id']}"
    return str(emoji.get("name") or "")


def to_message(data: dict) -> FluxerMessage:
    """Discord 兼容 Message → FluxerMessage。"""
    if not isinstance(data, dict) or not data.get("id"):
        raise FluxerUnavailable("Fluxer 返回了无效消息")
    author = data.get("author") or {}
    ref = data.get("message_reference") or {}
    referenced = data.get("referenced_message") or {}
    return FluxerMessage(
        id=str(data["id"]),
        channel_id=str(data.get("channel_id") or ""),
        author_id=str(author.get("id") or ""),
        content=str(data.get("content") or ""),
        created_at=_iso(data.get("timestamp")) or _iso(datetime.now(UTC).isoformat()),
        edited_at=_iso(data.get("edited_timestamp")),
        reply_to=str(ref["message_id"]) if ref.get("message_id") else None,
        reply_to_author_id=str((referenced.get("author") or {}).get("id") or "")
        or None,
        nonce=str(data["nonce"]) if data.get("nonce") is not None else None,
        reactions=[
            {
                "emoji": emoji_key(r.get("emoji")),
                "count": int(r.get("count") or 0),
                "me": bool(r.get("me")),
                "user_ids": None,
            }
            for r in data.get("reactions") or []
            if int(r.get("count") or 0) > 0
        ],
        attachments=[
            {
                "id": str(a.get("id")),
                "url": a.get("url"),
                "filename": a.get("filename") or "file",
                "content_type": a.get("content_type") or "application/octet-stream",
                "size": int(a.get("size") or 0),
                "width": a.get("width"),
                "height": a.get("height"),
            }
            for a in data.get("attachments") or []
        ],
        author={
            "username": author.get("username"),
            "global_name": author.get("global_name"),
            "avatar": author.get("avatar"),
        },
    )


class NativeChat:
    """RealFluxer 的聊天部分（混入 RealFluxer）。"""

    async def chat_messages(
        self,
        token,
        user_id,
        channel_id,
        *,
        before=None,
        after=None,
        around=None,
        limit=50,
    ):
        data = await request(
            "GET",
            f"/channels/{_q(channel_id)}/messages",
            token=token,
            params={
                "limit": max(1, min(int(limit or 50), 100)),
                "before": before,
                "after": after,
                "around": around,
            },
        )
        items = [to_message(m) for m in data or []]
        # Discord 兼容接口按新到旧返回（after 时也一样），统一成正序
        items.sort(key=lambda m: int(m.id) if m.id.isdigit() else 0)
        return items

    async def chat_message(self, token, user_id, channel_id, message_id):
        try:
            data = await request(
                "GET",
                f"/channels/{_q(channel_id)}/messages/{_q(message_id)}",
                token=token,
            )
        except FluxerNotFound:
            return None
        return to_message(data)

    async def chat_send(self, token, user_id, channel_id, *, content, nonce, reply_to):
        body: dict = {"content": content}
        if nonce:
            body["nonce"] = nonce
            body["enforce_nonce"] = True
        if reply_to:
            body["message_reference"] = {
                "message_id": reply_to,
                "fail_if_not_exists": False,
            }
            body["allowed_mentions"] = {"parse": ["users"], "replied_user": True}
        data = await request(
            "POST", f"/channels/{_q(channel_id)}/messages", token=token, json=body
        )
        return to_message(data)

    async def chat_edit(self, token, user_id, channel_id, message_id, *, content):
        data = await request(
            "PATCH",
            f"/channels/{_q(channel_id)}/messages/{_q(message_id)}",
            token=token,
            json={"content": content},
        )
        return to_message(data)

    async def chat_delete(
        self, token, user_id, channel_id, message_id, *, deleted_state, as_moderator
    ):
        await request(
            "DELETE",
            f"/channels/{_q(channel_id)}/messages/{_q(message_id)}",
            token=token,
            bot=as_moderator,
        )

    async def chat_react(self, token, user_id, channel_id, message_id, emoji, *, on):
        await request(
            "PUT" if on else "DELETE",
            f"/channels/{_q(channel_id)}/messages/{_q(message_id)}/reactions/{_q(emoji)}/@me",
            token=token,
        )

    async def chat_ack(self, token, user_id, channel_id, message_id):
        await request(
            "POST",
            f"/channels/{_q(channel_id)}/messages/{_q(message_id)}/ack",
            token=token,
            json={},
        )

    async def chat_typing(self, token, user_id, channel_id):
        await request("POST", f"/channels/{_q(channel_id)}/typing", token=token)

    async def chat_dm_channels(self, token, user_id):
        data = await request("GET", "/users/@me/channels", token=token)
        result = []
        for ch in data or []:
            # type 1 = 私信（不做群组）
            if int(ch.get("type") or 0) != 1:
                continue
            peers = [
                str(r.get("id"))
                for r in ch.get("recipients") or []
                if str(r.get("id")) != user_id
            ]
            if not peers:
                continue
            result.append(
                {
                    "channel_id": str(ch["id"]),
                    "peer_user_id": peers[0],
                    "created_at": None,
                    "last_message_id": str(ch["last_message_id"])
                    if ch.get("last_message_id")
                    else None,
                }
            )
        return result

    async def chat_open_dm(self, token, user_id, peer_user_id):
        data = await request(
            "POST",
            "/users/@me/channels",
            token=token,
            json={"recipient_id": peer_user_id},
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise FluxerUnavailable("Fluxer 没有返回私信频道")
        return str(data["id"])
