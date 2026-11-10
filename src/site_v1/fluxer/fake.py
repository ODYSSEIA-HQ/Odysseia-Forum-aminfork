"""FakeFluxer：FLUXER_FAKE=1 时代替真实 Fluxer（本地开发、契约测试、pytest）。

- 账号：site_fake_credential（用户名、邮箱、scrypt 密码哈希）+ 本地 user 投影；
  用户 ID 是 18 位数字字符串，形状同真实雪花 ID。
- 令牌：flx_fake_ + 随机串，Redis 键 site:fake:tok:{sha256} → user_id，30 天。
- 不支持真实 Discord 交换（discord_exchange 抛 FluxerUnavailable）。
- 未读汇总、聊天目标：测试种子（unread_seed / dm_seed）优先；否则按本地表算
  （site_chat_read_state + site_chat_message），聊天目标按作品 ID 构造假频道。
- 聊天消息：存 site_chat_message（软删除留占位），私信频道存 site_chat_dm_channel。
"""

import hashlib
import hmac
import secrets
import time

from sqlalchemy import func, select

from site_v1.fluxer.port import (
    AccountExists,
    FluxerForbidden,
    DiscordUser,
    FluxerIdentity,
    FluxerMessage,
    FluxerNotFound,
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


def _now():
    from shared.time_utils import utc_now

    return utc_now()


def new_user_id() -> str:
    """18 位数字，首位非 0（满足 V2 的 [1-9][0-9]{0,18} 校验）。"""
    return str(secrets.randbelow(9 * 10**17) + 10**17)


class FakeFluxer:
    """FluxerPort 的假实现。"""

    def __init__(self, session_factory=None, redis=None):
        self._session_factory = session_factory
        self._redis = redis
        # 聊天外层的种子数据（测试用）：没有种子时未读为空、私信一律不存在
        self.unread_seed: dict[str, UnreadSummary] = {}
        """user_id → 未读汇总。"""
        self.dm_seed: dict[str, dict] = {}
        """私信频道 ID → {members: [user_id], member_count?}。"""

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
        if user_id in self.unread_seed:
            return self.unread_seed[user_id]
        from models.site_chat import SiteChatReadState

        works: list[dict] = []
        channels: list[dict] = []
        dms: list[dict] = []
        async with self.sessions() as session:
            reads = {
                r.channel_id: r
                for r in (
                    await session.execute(
                        select(SiteChatReadState).where(SiteChatReadState.user_id == user_id)
                    )
                ).scalars()
            }
            for channel_id, row in reads.items():
                if row.target_kind == "dm":
                    continue
                entry = await self._unread_entry(session, channel_id, user_id, row.last_read_message_id)
                if row.target_kind == "work":
                    works.append({"thread_id": channel_id, **entry})
                elif row.target_kind == "channel":
                    channels.append({"channel_id": channel_id, **entry})
            for dm in await self._dm_rows(session, user_id):
                read = reads.get(dm.id)
                entry = await self._unread_entry(
                    session, dm.id, user_id, read.last_read_message_id if read else None
                )
                dms.append({"channel_id": dm.id, **entry})
        return UnreadSummary(works=works, dms=dms, channels=channels)

    async def _unread_entry(self, session, channel_id: str, user_id: str, last_read: str | None) -> dict:
        """未读数：已读位置之后、不是自己发的、没删的；@我 + 回复我的算 mentions。"""
        from models.site_chat import SiteChatMessage

        q = select(SiteChatMessage).where(SiteChatMessage.channel_id == channel_id)
        if last_read and last_read.isdigit():
            q = q.where(SiteChatMessage.id > int(last_read))
        rows = (await session.execute(q.order_by(SiteChatMessage.id))).scalars().all()
        reply_ids = {r.reply_to for r in rows if r.reply_to}
        mine = set()
        if reply_ids:
            mine = set(
                (
                    await session.execute(
                        select(SiteChatMessage.id).where(
                            SiteChatMessage.id.in_(reply_ids),
                            SiteChatMessage.author_id == user_id,
                        )
                    )
                ).scalars()
            )
        unread = mentions = 0
        for m in rows:
            if m.author_id == user_id or m.deleted_state:
                continue
            unread += 1
            if f"<@{user_id}>" in (m.content or "") or (m.reply_to and m.reply_to in mine):
                mentions += 1
        last = (
            await session.execute(
                select(func.max(SiteChatMessage.created_at)).where(
                    SiteChatMessage.channel_id == channel_id
                )
            )
        ).scalar_one_or_none()
        from site_v1.dto import iso_utc

        return {"unread": unread, "mentions": mentions, "last_message_at": iso_utc(last)}

    async def _dm_rows(self, session, user_id: str):
        from models.site_chat import SiteChatDmChannel

        return (
            (
                await session.execute(
                    select(SiteChatDmChannel).where(
                        (SiteChatDmChannel.user_low == user_id)
                        | (SiteChatDmChannel.user_high == user_id)
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _dm_members(self, session, channel_id: str) -> list[str] | None:
        """私信频道的成员；不是私信频道返回 None。测试种子 dm_seed 优先。"""
        from models.site_chat import SiteChatDmChannel

        seeded = self.dm_seed.get(channel_id)
        if seeded:
            return list(seeded["members"])
        row = await session.get(SiteChatDmChannel, channel_id)
        if row is None:
            return None
        return [row.user_low, row.user_high]

    async def chat_target_info(
        self, token: str | None, kind: str, target_id: str
    ) -> dict | None:
        if kind == "dm":
            # 私信要是会话成员：按令牌解析出的身份比对成员
            identity = await self.resolve_session(token) if token else None
            async with self.sessions() as session:
                members = await self._dm_members(session, target_id)
            if not members or identity is None or identity.user_id not in members:
                return None
            peer = next((m for m in members if m != identity.user_id), None)
            seeded = self.dm_seed.get(target_id) or {}
            return {
                "channel_id": target_id,
                "member_count": seeded.get("member_count", len(members)),
                "peer_user_id": peer,
            }
        # 作品交流区：假频道，成员数固定，ID 由目标派生，保证同一目标稳定
        return {
            "channel_id": f"fake-{kind}-{target_id}",
            "member_count": 1,
            "at": int(time.time()),
        }

    # ---------------- 聊天消息 ----------------

    async def _check_channel(self, session, user_id: str | None, channel_id: str) -> None:
        """私信频道只有成员能读写；其他频道（作品、公共聊天区）谁都行（可见性 Forum 已判断）。"""
        members = await self._dm_members(session, channel_id)
        if members is not None and (user_id is None or user_id not in members):
            raise FluxerNotFound(channel_id)

    @staticmethod
    def _to_message(row, user_id: str | None, reply_author: str | None) -> FluxerMessage:
        from site_v1.dto import iso_utc

        reactions = [
            {"emoji": emoji, "count": len(users), "me": bool(user_id and user_id in users), "user_ids": list(users)}
            for emoji, users in (row.reactions or {}).items()
            if users
        ]
        return FluxerMessage(
            id=str(row.id),
            channel_id=row.channel_id,
            author_id=row.author_id,
            content=row.content or "",
            created_at=iso_utc(row.created_at),
            edited_at=iso_utc(row.edited_at),
            reply_to=str(row.reply_to) if row.reply_to else None,
            reply_to_author_id=reply_author,
            nonce=row.nonce,
            deleted_state=row.deleted_state,
            reactions=reactions,
        )

    async def _messages_out(self, session, rows, user_id) -> list[FluxerMessage]:
        from models.site_chat import SiteChatMessage

        reply_ids = {r.reply_to for r in rows if r.reply_to}
        authors = {}
        if reply_ids:
            authors = dict(
                (
                    await session.execute(
                        select(SiteChatMessage.id, SiteChatMessage.author_id).where(
                            SiteChatMessage.id.in_(reply_ids)
                        )
                    )
                ).all()
            )
        return [self._to_message(r, user_id, authors.get(r.reply_to)) for r in rows]

    async def _row(self, session, channel_id: str, message_id: str, *, lock=False):
        from models.site_chat import SiteChatMessage

        if not isinstance(message_id, str) or not message_id.isdigit() or len(message_id) > 18:
            return None
        row = await session.get(SiteChatMessage, int(message_id), with_for_update=lock or None)
        if row is None or row.channel_id != channel_id:
            return None
        return row

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
    ) -> list[FluxerMessage]:
        from models.site_chat import SiteChatMessage

        limit = max(1, min(int(limit or 50), 100))
        async with self.sessions() as session:
            await self._check_channel(session, user_id, channel_id)
            base = select(SiteChatMessage).where(SiteChatMessage.channel_id == channel_id)
            cursor = before or after or around
            if cursor and await self._row(session, channel_id, cursor) is None:
                raise FluxerNotFound(cursor)
            if before:
                rows = (
                    await session.execute(
                        base.where(SiteChatMessage.id < int(before))
                        .order_by(SiteChatMessage.id.desc())
                        .limit(limit)
                    )
                ).scalars().all()[::-1]
            elif after:
                rows = (
                    await session.execute(
                        base.where(SiteChatMessage.id > int(after))
                        .order_by(SiteChatMessage.id)
                        .limit(limit)
                    )
                ).scalars().all()
            elif around:
                center = int(around)
                half = limit // 2
                older = (
                    await session.execute(
                        base.where(SiteChatMessage.id < center)
                        .order_by(SiteChatMessage.id.desc())
                        .limit(half)
                    )
                ).scalars().all()[::-1]
                newer = (
                    await session.execute(
                        base.where(SiteChatMessage.id >= center)
                        .order_by(SiteChatMessage.id)
                        .limit(limit - len(older))
                    )
                ).scalars().all()
                rows = list(older) + list(newer)
            else:
                rows = (
                    await session.execute(
                        base.order_by(SiteChatMessage.id.desc()).limit(limit)
                    )
                ).scalars().all()[::-1]
            return await self._messages_out(session, list(rows), user_id)

    async def chat_message(self, token, user_id, channel_id, message_id):
        async with self.sessions() as session:
            await self._check_channel(session, user_id, channel_id)
            row = await self._row(session, channel_id, message_id)
            if row is None:
                return None
            return (await self._messages_out(session, [row], user_id))[0]

    async def chat_send(self, token, user_id, channel_id, *, content, nonce, reply_to):
        from models.site_chat import SiteChatMessage

        async with self.sessions() as session:
            await self._check_channel(session, user_id, channel_id)
            row = SiteChatMessage(
                channel_id=channel_id,
                author_id=user_id,
                content=content,
                nonce=nonce,
                reply_to=int(reply_to) if reply_to and reply_to.isdigit() else None,
            )
            session.add(row)
            await session.flush()
            out = (await self._messages_out(session, [row], user_id))[0]
            await session.commit()
            return out

    async def chat_edit(self, token, user_id, channel_id, message_id, *, content):
        from shared.time_utils import utc_now

        async with self.sessions() as session:
            await self._check_channel(session, user_id, channel_id)
            row = await self._row(session, channel_id, message_id, lock=True)
            if row is None:
                raise FluxerNotFound(message_id)
            if row.author_id != user_id:
                raise FluxerForbidden("只能编辑自己的消息")
            row.content = content
            row.edited_at = utc_now()
            await session.flush()
            out = (await self._messages_out(session, [row], user_id))[0]
            await session.commit()
            return out

    async def chat_delete(self, token, user_id, channel_id, message_id, *, deleted_state, as_moderator):
        async with self.sessions() as session:
            await self._check_channel(session, user_id, channel_id)
            row = await self._row(session, channel_id, message_id, lock=True)
            if row is None:
                raise FluxerNotFound(message_id)
            if not as_moderator and row.author_id != user_id:
                raise FluxerForbidden("只能删除自己的消息")
            # 软删除：原文留着（申诉、管理日志用），对外只给占位
            row.deleted_state = row.deleted_state or deleted_state
            await session.commit()

    async def chat_react(self, token, user_id, channel_id, message_id, emoji, *, on):
        from sqlalchemy.orm.attributes import flag_modified

        async with self.sessions() as session:
            await self._check_channel(session, user_id, channel_id)
            row = await self._row(session, channel_id, message_id, lock=True)
            if row is None:
                raise FluxerNotFound(message_id)
            reactions = {k: list(v) for k, v in (row.reactions or {}).items()}
            users = reactions.get(emoji, [])
            if on and user_id not in users:
                users.append(user_id)
            elif not on and user_id in users:
                users.remove(user_id)
            if users:
                reactions[emoji] = users
            else:
                reactions.pop(emoji, None)
            row.reactions = reactions
            flag_modified(row, "reactions")
            await session.commit()

    async def chat_ack(self, token, user_id, channel_id, message_id) -> None:
        # 已读位置 Forum 自己记（site_chat_read_state），假 Fluxer 不用再存
        return None

    async def chat_typing(self, token, user_id, channel_id) -> None:
        return None

    async def chat_dm_channels(self, token, user_id) -> list[dict]:
        from models.site_chat import SiteChatMessage
        from site_v1.dto import iso_utc

        async with self.sessions() as session:
            rows = await self._dm_rows(session, user_id)
            result = []
            for row in rows:
                last = (
                    await session.execute(
                        select(func.max(SiteChatMessage.id)).where(
                            SiteChatMessage.channel_id == row.id
                        )
                    )
                ).scalar_one_or_none()
                result.append(
                    {
                        "channel_id": row.id,
                        "peer_user_id": row.user_high if row.user_low == user_id else row.user_low,
                        "created_at": iso_utc(row.created_at),
                        "last_message_id": str(last) if last else None,
                    }
                )
            for channel_id, seeded in self.dm_seed.items():
                members = seeded.get("members") or []
                if user_id in members and not any(r["channel_id"] == channel_id for r in result):
                    peer = next((m for m in members if m != user_id), user_id)
                    result.append(
                        {"channel_id": channel_id, "peer_user_id": peer, "created_at": None, "last_message_id": None}
                    )
            return result

    async def chat_open_dm(self, token, user_id, peer_user_id) -> str:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from models.site_chat import SiteChatDmChannel

        low, high = sorted([user_id, peer_user_id])
        async with self.sessions() as session:
            await session.execute(
                pg_insert(SiteChatDmChannel)
                .values(id=new_user_id(), user_low=low, user_high=high, created_at=_now())
                .on_conflict_do_nothing(constraint="uq_site_chat_dm_pair")
            )
            channel_id = (
                await session.execute(
                    select(SiteChatDmChannel.id).where(
                        SiteChatDmChannel.user_low == low, SiteChatDmChannel.user_high == high
                    )
                )
            ).scalar_one()
            await session.commit()
            return channel_id

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
