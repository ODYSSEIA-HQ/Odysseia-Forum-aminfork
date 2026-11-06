"""FakeFluxer：FLUXER_FAKE=1 时代替真实 Fluxer（本地开发、契约测试、pytest）。

- 账号：site_fake_credential（用户名、邮箱、scrypt 密码哈希）+ 本地 user 投影；
  用户 ID 是 18 位数字字符串，形状同真实雪花 ID。
- 令牌：flx_fake_ + 随机串，Redis 键 site:fake:tok:{sha256} → user_id，30 天。
- 不支持真实 Discord 交换（discord_exchange 抛 FluxerUnavailable）。
- 未读汇总、聊天目标：返回空 / 按作品 ID 构造的假频道。
"""

import hashlib
import hmac
import secrets
import time

from sqlalchemy import func, select

from site_v1.fluxer.port import (
    AccountExists,
    DiscordUser,
    FluxerIdentity,
    FluxerSession,
    FluxerUnavailable,
    InvalidCredentials,
    Origin,
    UnreadSummary,
)

TOKEN_PREFIX = "site:fake:tok:"
TOKEN_TTL = 30 * 86400
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False


def new_user_id() -> str:
    """18 位数字，首位非 0（满足 V2 的 [1-9][0-9]{0,18} 校验）。"""
    return str(secrets.randbelow(9 * 10**17) + 10**17)


class FakeFluxer:
    """FluxerPort 的假实现。"""

    def __init__(self, session_factory=None, redis=None):
        self._session_factory = session_factory
        self._redis = redis

    @property
    def sessions(self):
        if self._session_factory is None:
            from site_v1.deps import get_session_factory

            return get_session_factory()
        return self._session_factory

    @property
    def redis(self):
        if self._redis is None:
            from shared.redis_client import RedisManager

            return RedisManager.get_client()
        return self._redis

    async def _issue(self, identity: FluxerIdentity) -> FluxerSession:
        token = "flx_fake_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        await self.redis.set(TOKEN_PREFIX + digest, identity.user_id, ex=TOKEN_TTL)
        return FluxerSession(
            token=token,
            identity=FluxerIdentity(
                user_id=identity.user_id,
                username=identity.username,
                display_name=identity.display_name,
                avatar_url=identity.avatar_url,
                discord_user_id=identity.discord_user_id,
                session_id_hash=digest[:32],
            ),
        )

    async def _identity_of(self, session, user_id: str) -> FluxerIdentity | None:
        from models.discord_identity import DiscordIdentity
        from models.user import User

        user = await session.get(User, user_id)
        if user is None or user.merged_into_id:
            return None
        discord_id = (
            await session.execute(
                select(DiscordIdentity.discord_user_id).where(
                    DiscordIdentity.user_id == user_id,
                    DiscordIdentity.is_current.is_(True),
                )
            )
        ).scalar_one_or_none()
        return FluxerIdentity(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            discord_user_id=discord_id,
        )

    # ---------------- FluxerPort ----------------

    async def password_login(
        self, login: str, password: str, origin: Origin
    ) -> FluxerSession:
        from models.site_account import SiteFakeCredential

        key = login.strip().lower()
        async with self.sessions() as session:
            cred = (
                (
                    await session.execute(
                        select(SiteFakeCredential).where(
                            (func.lower(SiteFakeCredential.username) == key)
                            | (func.lower(SiteFakeCredential.email) == key)
                        )
                    )
                )
                .scalars()
                .first()
            )
            if cred is None or not verify_password(password, cred.password_hash):
                raise InvalidCredentials()
            identity = await self._identity_of(session, cred.user_id)
        if identity is None:
            raise InvalidCredentials()
        return await self._issue(identity)

    async def register(
        self,
        username: str,
        password: str,
        *,
        email: str | None,
        display_name: str | None,
        origin: Origin,
    ) -> FluxerSession:
        from sqlalchemy.exc import IntegrityError

        from models.site_account import SiteFakeCredential
        from models.user import User

        async with self.sessions() as session:
            taken = (
                await session.execute(
                    select(SiteFakeCredential.username).where(
                        func.lower(SiteFakeCredential.username) == username.lower()
                    )
                )
            ).first() or (
                await session.execute(
                    select(User.id).where(func.lower(User.username) == username.lower())
                )
            ).first()
            if taken:
                raise AccountExists("username")
            if email:
                used = (
                    await session.execute(
                        select(SiteFakeCredential.user_id).where(
                            func.lower(SiteFakeCredential.email) == email.lower()
                        )
                    )
                ).first()
                if used:
                    raise AccountExists("email")
            user_id = new_user_id()
            session.add(
                User(id=user_id, username=username, display_name=display_name or None)
            )
            await session.flush()
            session.add(
                SiteFakeCredential(
                    user_id=user_id,
                    username=username,
                    email=email,
                    password_hash=hash_password(password),
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                raise AccountExists("username") from exc
        return await self._issue(
            FluxerIdentity(
                user_id=user_id, username=username, display_name=display_name or None
            )
        )

    async def resolve_session(self, token: str) -> FluxerIdentity | None:
        if not isinstance(token, str) or not token.startswith("flx_fake_"):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        user_id = await self.redis.get(TOKEN_PREFIX + digest)
        if not user_id:
            return None
        async with self.sessions() as session:
            identity = await self._identity_of(session, user_id)
        if identity is None:
            return None
        return FluxerIdentity(
            user_id=identity.user_id,
            username=identity.username,
            display_name=identity.display_name,
            avatar_url=identity.avatar_url,
            discord_user_id=identity.discord_user_id,
            session_id_hash=digest[:32],
        )

    async def revoke_session(self, token: str) -> None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        await self.redis.delete(TOKEN_PREFIX + digest)

    async def discord_exchange(
        self, discord_user: DiscordUser, exchange_id: str, origin: Origin
    ) -> FluxerSession:
        raise FluxerUnavailable("假 Fluxer 不支持 Discord 登录")

    async def update_profile(
        self, token: str, user_id: str, *, display_name: str
    ) -> FluxerIdentity:
        from models.user import User
        from shared.time_utils import utc_now

        async with self.sessions() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise FluxerUnavailable("账号不存在")
            user.display_name = display_name
            user.updated_at = utc_now()
            await session.commit()
            identity = await self._identity_of(session, user_id)
        assert identity is not None
        return identity

    async def unread_summary(self, token: str, user_id: str) -> UnreadSummary:
        return UnreadSummary()

    async def chat_target_info(
        self, token: str | None, kind: str, target_id: str
    ) -> dict | None:
        # 假频道：成员数固定，ID 由目标派生，保证同一目标稳定
        return {
            "channel_id": f"fake-{kind}-{target_id}",
            "member_count": 1,
            "at": int(time.time()),
        }

    # ---------------- 只给种子脚本用 ----------------

    async def ensure_account(
        self,
        username: str,
        password: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """幂等建号（种子用）：已有同名账号时重设密码，返回用户 ID。"""
        from models.site_account import SiteFakeCredential
        from models.user import User

        async with self.sessions() as session:
            cred = (
                (
                    await session.execute(
                        select(SiteFakeCredential).where(
                            func.lower(SiteFakeCredential.username) == username.lower()
                        )
                    )
                )
                .scalars()
                .first()
            )
            if cred is None:
                user_id = user_id or new_user_id()
                if await session.get(User, user_id) is None:
                    session.add(
                        User(id=user_id, username=username, display_name=display_name)
                    )
                    await session.flush()
                cred = SiteFakeCredential(
                    user_id=user_id,
                    username=username,
                    email=email,
                    password_hash=hash_password(password),
                )
                session.add(cred)
            else:
                cred.password_hash = hash_password(password)
                cred.email = email
                user = await session.get(User, cred.user_id)
                if user is not None:
                    user.display_name = display_name
            result = cred.user_id
            await session.commit()
            return result


__all__ = ["FakeFluxer", "hash_password", "verify_password", "DiscordUser"]
