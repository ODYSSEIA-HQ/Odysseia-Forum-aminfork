"""协议 1.2 服务端会话：Redis 存会话，浏览器只拿一个随机 sid Cookie。

- Redis 键 site:sess:{sha256(sid)}，值 JSON：
  {"user_id": str|None, "csrf": str, "token": str|None, "created_at": int}
  token 是 Fluxer 会话令牌，只存在这里，从不下发给浏览器。
- 匿名会话（user_id=None）只存 csrf，由 GET /session 或 Discord 授权入口创建。
- 登录、注册时轮换：删旧会话、建新 sid 和新 csrf。
- site:user_sess:{user_id} 记这个账号的会话（哈希后的 sid），给「踢下线」用。
"""

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from site_v1.config import SiteConfig

KEY_PREFIX = "site:sess:"
USER_INDEX_PREFIX = "site:user_sess:"


def _digest(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


@dataclass
class SessionData:
    sid: str
    csrf: str
    user_id: str | None = None
    token: str | None = None
    created_at: int = 0
    is_new: bool = False
    """本次请求新建的（需要 Set-Cookie）。"""

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None

    def csrf_matches(self, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(self.csrf, presented)


class SessionStore:
    """Redis 会话读写。redis 是 redis.asyncio 客户端（decode_responses=True）。"""

    def __init__(self, redis, config: SiteConfig):
        self.redis = redis
        self.config = config

    async def load(self, sid: str | None) -> SessionData | None:
        if not sid or len(sid) > 128:
            return None
        raw = await self.redis.get(KEY_PREFIX + _digest(sid))
        if not raw:
            return None
        data = json.loads(raw)
        return SessionData(
            sid=sid,
            csrf=data["csrf"],
            user_id=data.get("user_id"),
            token=data.get("token"),
            created_at=int(data.get("created_at") or 0),
        )

    async def save(self, session: SessionData) -> None:
        ttl = (
            self.config.session_ttl_seconds
            if session.authenticated
            else self.config.anon_session_ttl_seconds
        )
        payload = json.dumps(
            {
                "user_id": session.user_id,
                "csrf": session.csrf,
                "token": session.token,
                "created_at": session.created_at,
            }
        )
        digest = _digest(session.sid)
        await self.redis.set(KEY_PREFIX + digest, payload, ex=ttl)
        if session.user_id:
            index = USER_INDEX_PREFIX + session.user_id
            await self.redis.sadd(index, digest)
            await self.redis.expire(index, self.config.session_ttl_seconds)

    async def create_anonymous(self) -> SessionData:
        session = SessionData(
            sid=new_token(),
            csrf=new_token(24),
            created_at=int(time.time()),
            is_new=True,
        )
        await self.save(session)
        return session

    async def create_authenticated(
        self, user_id: str, token: str, *, replace: SessionData | None = None
    ) -> SessionData:
        """登录成功：轮换 sid 和 csrf，旧会话（匿名或别的账号）删掉。"""
        if replace is not None:
            await self.destroy(replace)
        session = SessionData(
            sid=new_token(),
            csrf=new_token(24),
            user_id=user_id,
            token=token,
            created_at=int(time.time()),
            is_new=True,
        )
        await self.save(session)
        return session

    async def destroy(self, session: SessionData) -> None:
        digest = _digest(session.sid)
        await self.redis.delete(KEY_PREFIX + digest)
        if session.user_id:
            await self.redis.srem(USER_INDEX_PREFIX + session.user_id, digest)

    async def destroy_all_for_user(self, user_id: str) -> int:
        """踢掉某账号的全部会话（站务封号、合并账号时用）。"""
        index = USER_INDEX_PREFIX + user_id
        digests = await self.redis.smembers(index)
        if digests:
            await self.redis.delete(*[KEY_PREFIX + d for d in digests])
        await self.redis.delete(index)
        return len(digests)

    # ---------------- Cookie ----------------

    def cookie_header(self, session: SessionData) -> str:
        """Set-Cookie 值：HttpOnly; Secure（可配置）; SameSite=Lax; Path=/。"""
        ttl = (
            self.config.session_ttl_seconds
            if session.authenticated
            else self.config.anon_session_ttl_seconds
        )
        parts = [
            f"{self.config.cookie_name}={session.sid}",
            "HttpOnly",
            "SameSite=Lax",
            "Path=/",
            f"Max-Age={ttl}",
        ]
        if self.config.cookie_secure:
            parts.insert(2, "Secure")
        return "; ".join(parts)

    def clear_cookie_header(self) -> str:
        parts = [
            f"{self.config.cookie_name}=",
            "HttpOnly",
            "SameSite=Lax",
            "Path=/",
            "Max-Age=0",
        ]
        if self.config.cookie_secure:
            parts.insert(2, "Secure")
        return "; ".join(parts)
