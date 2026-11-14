"""身份头模式（网关与核心接入约定第 2 节）：校验 X-Ody-Principal，当前请求的 Principal。

SITE_V1_AUTH_MODE=principal 时，/api/v1 只认网关签的 X-Ody-Principal 头：
- 格式 base64url(JSON) + "." + base64url(HMAC-SHA256(secret, 前半段字符串))，base64url 不带填充；
- 签名用 hmac.compare_digest 比较，iat 是毫秒，|now − iat| ≤ 60 秒；
- 签名不对、过期、字段不对，一律当作没有这个头（中间件返回 401）。

字段以 odysseia-api packages/kit/src/principal.ts 的 Principal 为准。
"""

import base64
import binascii
import contextvars
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

HEADER = "x-ody-principal"
MAX_AGE_MS = 60_000
MODES = ("general", "r18")

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """只接受不带填充的 base64url。"""
    if not _B64URL.fullmatch(text):
        raise ValueError("不是不带填充的 base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict, secret: str) -> str:
    """签一个头（测试和本地调试用；生产由网关签）。"""
    body = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    )
    mac = hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(mac)}"


def _str_list(value) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # 本仓库统一用 naive UTC（shared.time_utils.utc_now）
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    mode: str
    show_r18g: bool
    permissions: frozenset[str]
    newcomer: bool
    muted_until: datetime | None
    blocked_user_ids: frozenset[str]
    blocked_tag_ids: tuple[str, ...]
    followed_tag_ids: tuple[str, ...] | None
    publish_suspended_until: datetime | None
    iat: int
    admin_ceiling: dict | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def effective_mode(self) -> str:
        """有效模式以网关给的 mode 为准；未登录一律 general。"""
        return "r18" if self.user_id and self.mode == "r18" else "general"


def _principal_of(data) -> Principal | None:
    if not isinstance(data, dict):
        return None
    iat = data.get("iat")
    if isinstance(iat, bool) or not isinstance(iat, (int, float)):
        return None
    user_id = data.get("userId")
    if user_id is not None and (
        not isinstance(user_id, str) or not user_id or len(user_id) > 64
    ):
        return None
    mode = data.get("mode", "general")
    if mode not in MODES:
        return None
    permissions = _str_list(data.get("permissions"))
    blocked_users = _str_list(data.get("blockedUserIds"))
    blocked_tags = _str_list(data.get("blockedTagIds"))
    followed = data.get("followedTagIds")
    followed_tags = None if followed is None else _str_list(followed)
    if None in (permissions, blocked_users, blocked_tags) or (
        followed is not None and followed_tags is None
    ):
        return None
    ceiling = data.get("adminCeiling")
    return Principal(
        user_id=user_id,
        mode=mode,
        show_r18g=data.get("showR18G") is True,
        permissions=frozenset(permissions),
        newcomer=data.get("newcomer") is True,
        muted_until=_parse_time(data.get("mutedUntil")),
        blocked_user_ids=frozenset(blocked_users),
        blocked_tag_ids=tuple(blocked_tags),
        followed_tag_ids=None if followed_tags is None else tuple(followed_tags),
        publish_suspended_until=_parse_time(data.get("publishSuspendedUntil")),
        iat=int(iat),
        admin_ceiling=ceiling if isinstance(ceiling, dict) else None,
        raw=data,
    )


def verify(
    token: str | None, secret: str, now_ms: int | None = None
) -> Principal | None:
    """校验头；签名不对、过期、格式不对返回 None。"""
    if not token or not secret or len(token) > 16384:
        return None
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    body, sig = parts
    try:
        given = _b64url_decode(sig)
        payload = _b64url_decode(body)
    except (ValueError, binascii.Error):
        return None
    expected = hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(given, expected):
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    principal = _principal_of(data)
    if principal is None:
        return None
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now - principal.iat) > MAX_AGE_MS:
        return None
    return principal


# ---------------- 当前请求的 Principal ----------------
# 中间件在身份头模式下设置；屏蔽名单、全站禁言这些「本人」的事实在网关那边算好了，
# social.blocked_user_ids / moderation_common.site_muted_until 查本人时合并进来。

_current: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "site_v1_principal", default=None
)


def current() -> Principal | None:
    return _current.get()


def set_current(principal: Principal | None):
    return _current.set(principal)


def reset_current(token) -> None:
    _current.reset(token)


def for_user(user_id: str | None) -> Principal | None:
    """当前请求的 Principal 正好是 user_id 本人时返回它。"""
    p = _current.get()
    return p if p is not None and user_id and p.user_id == user_id else None


# ---------------- 收件人最近一次的有效模式（实时推送用） ----------------
# 推送给别人时没有对方的 Principal：用对方最近一次请求带来的 mode / showR18G，
# 记不到（过期、没来过）一律按 general，保证 r18 内容不推给 general。

LAST_SEEN_PREFIX = "site:principal:last:"
LAST_SEEN_TTL = 6 * 3600


async def remember(redis, principal: Principal) -> None:
    if not principal.user_id:
        return
    value = json.dumps(
        {"mode": principal.effective_mode, "showR18G": principal.show_r18g}
    )
    await redis.set(LAST_SEEN_PREFIX + principal.user_id, value, ex=LAST_SEEN_TTL)


async def last_seen(redis, user_id: str) -> tuple[str, bool]:
    """(有效模式, showR18G)；没有记录时 (general, False)。"""
    try:
        raw = await redis.get(LAST_SEEN_PREFIX + user_id)
        data = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        data = {}
    mode = data.get("mode") if data.get("mode") in MODES else "general"
    return mode, bool(data.get("showR18G")) and mode == "r18"
