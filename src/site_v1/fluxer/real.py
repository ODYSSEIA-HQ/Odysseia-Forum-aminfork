"""真实 Fluxer：经 NATS RPC，复用 identity/fluxer_auth_service.py 的连接、缓存和失效机制。

- resolve_session：FluxerAuthService.resolve（带一小时正缓存和 epoch 失效）+ 本地 user 投影
  （同 identity/auth_runtime.resolve_principal 的 upsert）。
- revoke_session：同 api/v1/routers/auth.py 的 POST /v2/auth/logout。
- discord_exchange：同 api/v1/routers/auth.py 的 _create_login_response（fluxer 分支）里的
  rpc.auth.v1.external.exchange 和 user / discord_identity 投影，只是令牌不再交给浏览器。
- password_login / register / update_profile / unread_summary：Fluxer 还没有这些 RPC，
  这里按 docs/site_v1/fluxer-rpc.md 定的形状调用，Fluxer 实现前会得到 503。
"""

import json
import re
from types import SimpleNamespace

from fastapi import HTTPException

from identity.integration_rpc_error import IntegrationRpcError
from site_v1.fluxer.native_http import NativeChat
from site_v1.fluxer.port import (
    AccountExists,
    AccountUnavailable,
    DiscordUser,
    FluxerIdentity,
    FluxerSession,
    FluxerUnavailable,
    InvalidCredentials,
    MfaRequired,
    Origin,
    UnreadSummary,
)

_ID = re.compile(r"[1-9][0-9]{0,18}")

SUBJECT_PASSWORD_LOGIN = "rpc.auth.v1.password.login_service"
SUBJECT_ACCOUNT_REGISTER = "rpc.auth.v1.account.register_service"
SUBJECT_PROFILE_UPDATE = "rpc.identity.v1.profile.update_service"
SUBJECT_UNREAD_SUMMARY = "rpc.content.v1.read_states.summary_service"
SUBJECT_CHANNEL_INFO = "rpc.content.v1.channel.info_service"


def _origin(origin: Origin) -> dict:
    return {
        "ip": origin.ip,
        "user_agent": origin.user_agent[:512],
        "client_os": origin.client_os,
    }


def _identity(data: dict) -> FluxerIdentity:
    """校验 Fluxer 返回的身份字段，不合格当依赖异常。"""
    user_id = data.get("user_id")
    username = data.get("username")
    discord_id = data.get("discord_user_id")
    if (
        not isinstance(user_id, str)
        or not _ID.fullmatch(user_id)
        or not isinstance(username, str)
        or (
            discord_id is not None
            and (not isinstance(discord_id, str) or not _ID.fullmatch(discord_id))
        )
    ):
        raise FluxerUnavailable("Fluxer 返回了无效身份")
    return FluxerIdentity(
        user_id=user_id,
        username=username,
        display_name=data.get("display_name"),
        avatar_url=data.get("avatar_url"),
        discord_user_id=discord_id,
        session_id_hash=data.get("session_id_hash"),
    )


def _session(data: dict) -> FluxerSession:
    token = data.get("token")
    if not isinstance(token, str) or not token.startswith("flx_"):
        raise FluxerUnavailable("Fluxer 返回了无效令牌")
    return FluxerSession(token=token, identity=_identity(data))


class RealFluxer(NativeChat):
    """FluxerPort 的真实实现。聊天消息代转（chat_*）走 Fluxer 原生 HTTP 接口，见 native_http.py。"""

    def __init__(self, auth_service=None, session_factory=None):
        self._auth_service = auth_service
        self._session_factory = session_factory

    @property
    def auth(self):
        if self._auth_service is None:
            from identity.auth_runtime import get_auth_service

            self._auth_service = get_auth_service()
        return self._auth_service

    @property
    def sessions(self):
        if self._session_factory is None:
            from shared.database import AsyncSessionFactory

            self._session_factory = AsyncSessionFactory
        return self._session_factory

    async def _rpc(self, subject: str, **fields) -> dict:
        try:
            return await self.auth.rpc(subject, **fields)
        except HTTPException as exc:
            raise FluxerUnavailable(str(exc.detail)) from exc

    async def _project_user(
        self, identity: FluxerIdentity, *, bind_discord: bool = False
    ) -> None:
        """把 Fluxer 资料写进本地 user 投影（和 resolve_principal 一致）。"""
        from sqlalchemy import or_
        from sqlalchemy.dialects.postgresql import insert

        from models.discord_identity import DiscordIdentity
        from models.user import User

        values = {
            "id": identity.user_id,
            "username": identity.username,
            "display_name": identity.display_name,
            "avatar_url": identity.avatar_url,
        }
        try:
            async with self.sessions() as session:
                statement = insert(User).values(**values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[User.id],
                        set_=values,
                        where=or_(
                            User.username.is_distinct_from(statement.excluded.username),
                            User.display_name.is_distinct_from(
                                statement.excluded.display_name
                            ),
                            User.avatar_url.is_distinct_from(
                                statement.excluded.avatar_url
                            ),
                        ),
                    )
                )
                if bind_discord and identity.discord_user_id:
                    binding = insert(DiscordIdentity).values(
                        discord_user_id=identity.discord_user_id,
                        user_id=identity.user_id,
                        version=1,
                    )
                    await session.execute(binding.on_conflict_do_nothing())
                await session.commit()
        except Exception as exc:
            raise FluxerUnavailable("用户资料投影暂不可用") from exc

    # ---------------- 已有 RPC ----------------

    async def resolve_session(self, token: str) -> FluxerIdentity | None:
        try:
            data = await self.auth.resolve(token)
        except HTTPException as exc:
            raise FluxerUnavailable(str(exc.detail)) from exc
        if data is None:
            return None
        identity = _identity(data)
        await self._project_user(identity)
        return identity

    async def revoke_session(self, token: str) -> None:
        try:
            identity = await self.auth.resolve(token)
            await self.auth.rpc("rpc.auth.v1.session.revoke_current", token=token)
        except HTTPException as exc:
            raise FluxerUnavailable(str(exc.detail)) from exc
        if identity and identity.get("session_id_hash"):
            # 和 V2 退出一样，只失效这一个会话的解析缓存
            await self.auth.invalidated(
                SimpleNamespace(
                    subject="events.auth.v1.session_invalidated",
                    data=json.dumps(
                        {"session_id_hash": identity["session_id_hash"]}
                    ).encode(),
                )
            )

    async def discord_exchange(
        self, discord_user: DiscordUser, exchange_id: str, origin: Origin
    ) -> FluxerSession:
        from datetime import datetime, timezone

        data = await self._rpc(
            "rpc.auth.v1.external.exchange",
            exchange_id=exchange_id,
            discord_user_id=discord_user.id,
            verified_at=datetime.now(timezone.utc).isoformat(),
            profile={
                "username": discord_user.username,
                "display_name": discord_user.global_name,
                "avatar_url": discord_user.avatar_url,
            },
            origin=_origin(origin),
        )
        token = data.get("token")
        user_id = data.get("user_id")
        if (
            not isinstance(token, str)
            or not isinstance(user_id, str)
            or not _ID.fullmatch(user_id)
        ):
            raise FluxerUnavailable("Fluxer 交换结果无效")
        identity = FluxerIdentity(
            user_id=user_id,
            username=discord_user.username,
            display_name=discord_user.global_name,
            avatar_url=discord_user.avatar_url,
            discord_user_id=discord_user.id,
        )
        # 和 V2 回调一样：已有绑定指向别的账号时拒绝，等投影同步
        from sqlalchemy import select

        from models.discord_identity import DiscordIdentity

        async with self.sessions() as session:
            existing = (
                await session.execute(
                    select(DiscordIdentity).where(
                        DiscordIdentity.discord_user_id == discord_user.id
                    )
                )
            ).scalar_one_or_none()
            if existing and existing.user_id != user_id:
                raise FluxerUnavailable("身份绑定需要重新同步")
        await self._project_user(identity, bind_discord=True)
        return FluxerSession(token=token, identity=identity)

    # ---------------- 需要 Fluxer 新增的 RPC ----------------

    async def password_login(
        self, login: str, password: str, origin: Origin
    ) -> FluxerSession:
        try:
            data = await self.auth.rpc(
                SUBJECT_PASSWORD_LOGIN,
                login=login,
                password=password,
                origin=_origin(origin),
            )
        except IntegrationRpcError as exc:
            if exc.code == "INVALID_CREDENTIALS":
                raise InvalidCredentials() from exc
            if exc.code == "MFA_REQUIRED":
                raise MfaRequired() from exc
            if exc.code == "ACCOUNT_UNAVAILABLE":
                raise AccountUnavailable() from exc
            raise FluxerUnavailable(exc.code) from exc
        except HTTPException as exc:
            raise FluxerUnavailable(str(exc.detail)) from exc
        session = _session(data)
        await self._project_user(session.identity)
        return session

    async def register(
        self,
        username: str,
        password: str,
        *,
        email: str | None,
        display_name: str | None,
        origin: Origin,
    ) -> FluxerSession:
        try:
            data = await self.auth.rpc(
                SUBJECT_ACCOUNT_REGISTER,
                username=username,
                password=password,
                email=email,
                display_name=display_name,
                origin=_origin(origin),
            )
        except IntegrationRpcError as exc:
            if exc.code == "USERNAME_TAKEN":
                raise AccountExists("username") from exc
            if exc.code == "EMAIL_TAKEN":
                raise AccountExists("email") from exc
            raise FluxerUnavailable(exc.code) from exc
        except HTTPException as exc:
            raise FluxerUnavailable(str(exc.detail)) from exc
        session = _session(data)
        await self._project_user(session.identity)
        return session

    async def update_profile(
        self, token: str, user_id: str, *, display_name: str
    ) -> FluxerIdentity:
        try:
            data = await self.auth.rpc(
                SUBJECT_PROFILE_UPDATE,
                token=token,
                user_id=user_id,
                display_name=display_name,
            )
        except IntegrationRpcError as exc:
            raise FluxerUnavailable(exc.code) from exc
        except HTTPException as exc:
            raise FluxerUnavailable(str(exc.detail)) from exc
        identity = _identity(data)
        await self._project_user(identity)
        return identity

    async def unread_summary(self, token: str, user_id: str) -> UnreadSummary:
        try:
            data = await self.auth.rpc(
                SUBJECT_UNREAD_SUMMARY, token=token, user_id=user_id
            )
        except (IntegrationRpcError, HTTPException) as exc:
            raise FluxerUnavailable("未读汇总暂不可用") from exc
        return UnreadSummary(
            works=list(data.get("works") or []),
            dms=list(data.get("dms") or []),
            channels=list(data.get("channels") or []),
        )

    async def chat_target_info(
        self,
        token: str | None,
        kind: str,
        target_id: str,
        *,
        viewer_id: str | None = None,
    ) -> dict | None:
        try:
            data = await self.auth.rpc(
                SUBJECT_CHANNEL_INFO, token=token, kind=kind, target_id=target_id
            )
        except IntegrationRpcError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise FluxerUnavailable(exc.code) from exc
        except HTTPException as exc:
            raise FluxerUnavailable(str(exc.detail)) from exc
        return data
