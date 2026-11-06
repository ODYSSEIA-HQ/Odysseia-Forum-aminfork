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


class FluxerNotFound(FluxerError):
    """频道、消息不存在，或令牌对应的账号看不到（→ 404）。"""


class FluxerForbidden(FluxerError):
    """Fluxer 拒绝了这次操作（没权限、被 Fluxer 侧禁言等，→ 403）。"""


class FluxerUnauthorized(FluxerError):
    """Fluxer 说令牌无效（→ 401，下一个请求中间件会把会话降为匿名）。"""


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

    channels: list[dict] = field(default_factory=list)
    """[{channel_id(公共聊天区的 Fluxer 频道), unread, mentions, last_message_at}]，
    只列进过（有已读记录）的；由调用方映射到公共频道并按分级过滤。"""


@dataclass
class FluxerMessage:
    """Fluxer 频道里的一条消息（聊天代转层用，和 Fluxer 原生 Message 对应的子集）。

    - id 是可比较大小的数字字符串（雪花 ID / 假实现的自增 ID），越大越新；
    - reactions：[{emoji, count, me, user_ids}]，user_ids 只有假实现给（真实环境为 None），
      me 是取消息用的令牌对应的账号有没有点；
    - attachments：Fluxer 原生附件 [{id, url, filename, content_type, size, width, height}]，
      本站上传的附件不在这里（在 site_chat_message_meta）；
    - deleted_state：只有假实现会给（软删除留占位）；真实 Fluxer 删了就没了。
    """

    id: str
    channel_id: str
    author_id: str
    content: str
    created_at: str
    """ISO 8601 UTC。"""
    edited_at: str | None = None
    reply_to: str | None = None
    reply_to_author_id: str | None = None
    nonce: str | None = None
    deleted_state: str | None = None
    reactions: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    author: dict | None = None
    """Fluxer 给的作者资料 {username, global_name, avatar}，本地没有这个用户投影时用。"""

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FluxerMessage":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__ if k in data})


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

    # ---------------- 聊天消息代转（协议 3.5 NodeBB 路线面板接口） ----------------
    # token 是会话里的 Fluxer 令牌（真实实现用它调 Fluxer 原生 HTTP 接口），user_id 是同一个账号
    # （假实现按它判断成员、reactions.me）。channel_id 是 Fluxer 频道 ID：作品原生主帖、
    # 私信频道、公共聊天区频道。频道不存在 / 看不到 → FluxerNotFound。

    async def chat_messages(
        self,
        token: str | None,
        user_id: str | None,
        channel_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
        around: str | None = None,
        limit: int = 50,
    ) -> list[FluxerMessage]:
        """按时间正序返回最多 limit 条；三个游标都不带 = 最新一页。

        游标消息不在这个频道 → FluxerNotFound。"""
        ...

    async def chat_message(
        self, token: str | None, user_id: str | None, channel_id: str, message_id: str
    ) -> FluxerMessage | None:
        """取一条；不存在返回 None。"""
        ...

    async def chat_send(
        self,
        token: str,
        user_id: str,
        channel_id: str,
        *,
        content: str,
        nonce: str | None,
        reply_to: str | None,
    ) -> FluxerMessage:
        """发消息（本站附件不经过 Fluxer，见 site_chat_message_meta）。"""
        ...

    async def chat_edit(
        self, token: str, user_id: str, channel_id: str, message_id: str, *, content: str
    ) -> FluxerMessage:
        """改自己的消息。"""
        ...

    async def chat_delete(
        self,
        token: str,
        user_id: str,
        channel_id: str,
        message_id: str,
        *,
        deleted_state: str,
        as_moderator: bool,
    ) -> None:
        """删消息。as_moderator=True：删别人的（作者、卡贴管理员、站务；权限由 Forum 判断，
        真实实现用 FLUXER_BOT_TOKEN 的管理身份删）。"""
        ...

    async def chat_react(
        self,
        token: str,
        user_id: str,
        channel_id: str,
        message_id: str,
        emoji: str,
        *,
        on: bool,
    ) -> None:
        """加 / 取消自己的表情。"""
        ...

    async def chat_ack(
        self, token: str, user_id: str, channel_id: str, message_id: str
    ) -> None:
        """把已读位置同步给 Fluxer（Forum 自己也记一份，见 site_chat_read_state）。"""
        ...

    async def chat_typing(self, token: str, user_id: str, channel_id: str) -> None:
        """「正在输入」。"""
        ...

    async def chat_dm_channels(self, token: str, user_id: str) -> list[dict]:
        """这个账号的全部私信频道：[{channel_id, peer_user_id, created_at(ISO), last_message_id}]。"""
        ...

    async def chat_open_dm(self, token: str, user_id: str, peer_user_id: str) -> str:
        """和对方的私信频道 ID（没有就建）。隐私、屏蔽由 Forum 先判断。"""
        ...

