"""请求上下文（principal）：每个 /api/v1 请求由中间件建一个，挂在 request.state.site_ctx。

后面的领域只需要知道：
- ctx.viewer：当前账号（Viewer），未登录为 None；ctx.require_user() 未登录抛 401。
- ctx.effective_mode：general / r18（协议 1.1），响应头 X-Content-Mode 用它。
- ctx.show_r18g、ctx.visible_ratings()、ctx.can_view(rating, visibility)：可见性判断。
- ctx.fluxer_token：服务端会话里的 Fluxer 令牌（调 FluxerPort 用），绝不能下发。
- ctx.refresh_mode()：本请求里改了偏好或资格后重算有效模式（响应头跟着变）。
- viewer.to_v2_principal()：调用 V2 work/* 服务时用的 principal dict。
"""

from dataclasses import dataclass, field
from datetime import datetime

from site_v1 import mode as modes
from site_v1.errors import unauthenticated
from site_v1.session import SessionData

# 所有成员都有的权限键
MEMBER_PERMISSIONS = ("chat.attach", "chat.poll", "chat.fun")


@dataclass
class Prefs:
    content_mode: str = "general"
    show_r18g: bool = False
    blur_r18: bool = True
    blocked_tags: list[str] = field(default_factory=list)
    followed_tags: list[str] = field(default_factory=list)
    dm_privacy: str = "everyone"
    pinned_boards: list[str] = field(default_factory=list)


@dataclass
class RestrictedAccess:
    state: str = "none"
    source: str | None = None
    checked_at: datetime | None = None
    age_confirmed_at: datetime | None = None
    terms_version: str | None = None


@dataclass
class Viewer:
    """当前登录账号的快照（每个请求从库里现读，不跨请求缓存）。"""

    user_id: str
    username: str
    display_name: str | None
    avatar_url: str | None
    discord_user_id: str | None
    email: str | None
    created_at: datetime
    newcomer_until: datetime | None
    quiz_passed: bool
    permissions: frozenset[str]
    prefs: Prefs
    access: RestrictedAccess

    @property
    def id(self) -> str:
        return self.user_id

    @property
    def is_newcomer(self) -> bool:
        from shared.time_utils import utc_now

        return self.newcomer_until is not None and self.newcomer_until > utc_now()

    @property
    def discord_linked(self) -> bool:
        return self.discord_user_id is not None

    def has_permission(self, key: str) -> bool:
        return "site.admin" in self.permissions or key in self.permissions

    def to_v2_principal(self, mode: str) -> dict:
        """V2 work/* 服务要的 principal（只含身份和可见性字段）。

        V2 的 forums / config 等字段由调用方按需补上（见 api/v2/dependencies.current_member）。
        show_nsfw / can_view_abyss 按协议有效模式给，不再看 Discord 深渊身份组。
        """
        r18 = mode == modes.R18
        return {
            "id": self.user_id,
            "discord_user_id": self.discord_user_id,
            "username": self.username,
            "display_name": self.display_name,
            "global_name": self.display_name,
            "avatar_url": self.avatar_url,
            "show_nsfw": r18,
            "can_view_abyss": r18 and self.prefs.show_r18g,
            "is_admin": self.has_permission("works.manage_any"),
            "can_create_work": True,
            "can_speak": True,
        }


@dataclass
class SiteContext:
    session: SessionData | None = None
    viewer: Viewer | None = None
    effective_mode: str = modes.GENERAL
    pending_cookies: list[str] = field(default_factory=list)
    """要追加到响应上的 Set-Cookie（中间件写出）。"""

    @property
    def authenticated(self) -> bool:
        return self.viewer is not None

    @property
    def fluxer_token(self) -> str | None:
        return self.session.token if self.session and self.viewer else None

    @property
    def show_r18g(self) -> bool:
        return bool(self.viewer and self.viewer.prefs.show_r18g)

    def require_user(self) -> Viewer:
        if self.viewer is None:
            raise unauthenticated()
        return self.viewer

    def refresh_mode(self) -> str:
        v = self.viewer
        self.effective_mode = modes.effective_mode(
            v is not None,
            v.prefs.content_mode if v else None,
            v.access.state if v else None,
        )
        return self.effective_mode

    def visible_ratings(self) -> set[str]:
        return modes.visible_ratings(self.effective_mode, self.show_r18g)

    def can_view(self, rating: str, visibility: str) -> bool:
        return modes.can_view(self.effective_mode, self.show_r18g, rating, visibility)
