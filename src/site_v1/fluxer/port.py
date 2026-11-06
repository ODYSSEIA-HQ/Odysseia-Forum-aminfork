"""FluxerPort：/api/v1 层对 Fluxer 的全部外部依赖。

路由和服务只依赖这个 Protocol，不直接碰 NATS。两个实现：
- RealFluxer（site_v1/fluxer/real.py）：会话解析、注销、Discord 交换复用
  identity/fluxer_auth_service.py；密码登录、注册、改资料、未读汇总、聊天目标
  调用 Fluxer 侧**尚未提供**的新 RPC，形状见 docs/site_v1/fluxer-rpc.md。
- FakeFluxer（site_v1/fluxer/fake.py）：FLUXER_FAKE=1 时启用，账号和密码哈希存
  site_fake_credential，令牌是随机串存 Redis，不需要 NATS。

错误约定：
- FluxerUnavailable → 503 temporarily_unavailable（依赖超时、连接断开、返回格式不对）。
- InvalidCredentials → 422 validation_failed（fields.password）。
- AccountExists → 422 validation_failed（fields.username / fields.email）。
- MfaRequired → 403 forbidden，details.reason=mfa_required（第一期前端不处理 2FA）。
- AccountUnavailable → 403 forbidden（封禁、合并）。
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class FluxerError(Exception):
    """Fluxer 侧的业务失败基类。"""


class FluxerUnavailable(FluxerError):
    """依赖不可用或超时，可重试（→ 503）。"""


class InvalidCredentials(FluxerError):
    """用户名/邮箱或密码不对（不区分是哪一个）。"""


class AccountExists(FluxerError):
    """注册时用户名或邮箱已被占用。field 是 username 或 email。"""

    def __init__(self, field_name: str = "username"):
        self.field = field_name
        super().__init__(field_name)


class MfaRequired(FluxerError):
    """账号开了 2FA，密码登录还需要第二步（第一期不支持）。"""


class AccountUnavailable(FluxerError):
    """账号被封禁、停用或已合并。"""


@dataclass(frozen=True)
class Origin:
    """请求来源，转给 Fluxer 做风控（Fluxer 看到的 IP 都是 Forum 服务器）。"""

    ip: str = ""
    user_agent: str = ""
    client_os: str = "Forum Browser"


@dataclass(frozen=True)
class FluxerIdentity:
    """Fluxer 会话解析出的身份（rpc.auth.v1.session.resolve 的结果）。"""

    user_id: str
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    discord_user_id: str | None = None
    session_id_hash: str | None = None


@dataclass(frozen=True)
class FluxerSession:
    """登录、注册、Discord 交换得到的 Fluxer 会话。token 只存服务端会话。"""

    token: str
    identity: FluxerIdentity


@dataclass(frozen=True)
class DiscordUser:
    """Discord OAuth 读到的用户（/users/@me）。"""

    id: str
    username: str
    global_name: str | None = None
    avatar: str | None = None

    @property
    def avatar_url(self) -> str | None:
        if not self.avatar:
            return None
        return f"https://cdn.discordapp.com/avatars/{self.id}/{self.avatar}.png"


@dataclass(frozen=True)
class UnreadSummary:
    """GET /chat/unread-summary 的原始数据（作品、私信按目标汇总）。"""

    works: list[dict] = field(default_factory=list)
    """[{thread_id(原生主帖), unread, mentions, last_message_at}]，由调用方映射到作品并按有效模式过滤。"""

    dms: list[dict] = field(default_factory=list)
    """[{channel_id(私信频道), unread, mentions, last_message_at}]。

    last_message_at：ISO 8601 UTC 或毫秒时间戳，没有消息为 null。"""


@runtime_checkable
class FluxerPort(Protocol):
    """/api/v1 需要 Fluxer 提供的能力。"""

    async def password_login(
        self, login: str, password: str, origin: Origin
    ) -> FluxerSession:
        """用户名或邮箱 + 密码登录。"""
        ...

    async def register(
        self,
        username: str,
        password: str,
        *,
        email: str | None,
        display_name: str | None,
        origin: Origin,
    ) -> FluxerSession:
        """站内注册（registration=open 时才会调到）。"""
        ...

    async def resolve_session(self, token: str) -> FluxerIdentity | None:
        """令牌 → 身份；令牌无效返回 None（→ 当作未登录），依赖不可用抛 FluxerUnavailable。"""
        ...

    async def revoke_session(self, token: str) -> None:
        """注销当前 Fluxer 会话。失败不阻止本地退出（调用方吞掉 FluxerUnavailable）。"""
        ...

    async def discord_exchange(
        self, discord_user: DiscordUser, exchange_id: str, origin: Origin
    ) -> FluxerSession:
        """Discord OAuth 完成后换 Fluxer 会话（没有账号时 Fluxer 侧建号并绑定）。"""
        ...

    async def update_profile(
        self, token: str, user_id: str, *, display_name: str
    ) -> FluxerIdentity:
        """改显示名（Fluxer 是资料权威，本地投影随后更新）。"""
        ...

    async def unread_summary(self, token: str, user_id: str) -> UnreadSummary:
        """未读和 @ 汇总。"""
        ...

    async def chat_target_info(
        self, token: str | None, kind: str, target_id: str
    ) -> dict | None:
        """聊天目标信息；不存在（含：私信且令牌对应的账号不是成员）返回 None。

        kind=work：target_id 是原生主帖 ID，返回 {channel_id, member_count}。
        kind=dm：target_id 是私信频道 ID，返回 {channel_id, member_count, peer_user_id}。
        """
        ...
