"""/api/v1 层的配置：全部来自环境变量，读一次缓存在进程里。

测试可以改环境变量后调用 reload_config()。
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值 {raw!r}") from exc


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class SiteConfig:
    """/api/v1 层用到的全部配置项（见 docs/site_v1/README.md 的配置表）。"""

    fluxer_fake: bool
    """FLUXER_FAKE=1：用 FakeFluxer（本地账号表 + 随机令牌），不需要 NATS。"""

    cookie_name: str
    cookie_secure: bool
    session_ttl_seconds: int
    anon_session_ttl_seconds: int

    registration_mode: str
    """open / discord_only。"""

    terms_version: str
    require_discord_role: bool
    """Discord 正式成员身份组检查：真实环境默认开，fake 默认关。"""

    restricted_access_conditions: list[str]
    """R18 资格条件 ID，按顺序；可选 age_confirm、discord_role。"""

    newcomer_days: int
    test_hooks: bool
    frontend_base: str
    """前端站点根地址（同源时留空，跳转写相对路径 /login）。"""

    discord_client_id: str
    discord_client_secret: str
    discord_redirect_uri: str
    discord_role_ids: list[str] = field(default_factory=list)

    image_max_bytes: int = 10 * 1024 * 1024
    file_max_bytes: int = 50 * 1024 * 1024
    allowed_mimes: list[str] = field(default_factory=list)
    max_tags: int = 15

    @property
    def registration_open(self) -> bool:
        return self.registration_mode == "open"


@lru_cache(maxsize=1)
def get_config() -> SiteConfig:
    """读环境变量组装配置。"""
    fake = _bool("FLUXER_FAKE", False)
    mode = os.environ.get("SITE_REGISTRATION_MODE", "discord_only").strip()
    if mode not in {"open", "discord_only"}:
        raise RuntimeError("SITE_REGISTRATION_MODE 只能是 open 或 discord_only")
    require_role = _bool("SITE_REQUIRE_DISCORD_ROLE", not fake)
    return SiteConfig(
        fluxer_fake=fake,
        cookie_name=os.environ.get("SITE_COOKIE_NAME", "odysseia_sid"),
        cookie_secure=_bool("SITE_COOKIE_SECURE", True),
        session_ttl_seconds=_int("SITE_SESSION_TTL", 30 * 86400),
        anon_session_ttl_seconds=_int("SITE_ANON_SESSION_TTL", 86400),
        registration_mode=mode,
        terms_version=os.environ.get("SITE_TERMS_VERSION", "2026-10"),
        require_discord_role=require_role,
        restricted_access_conditions=_list(
            "SITE_RA_CONDITIONS",
            ["age_confirm", "discord_role"] if require_role else ["age_confirm"],
        ),
        newcomer_days=_int("SITE_NEWCOMER_DAYS", 5),
        test_hooks=_bool("SITE_TEST_HOOKS", False),
        frontend_base=os.environ.get("SITE_FRONTEND_BASE", "").rstrip("/"),
        discord_client_id=os.environ.get("SITE_DISCORD_CLIENT_ID", ""),
        discord_client_secret=os.environ.get("SITE_DISCORD_CLIENT_SECRET", ""),
        discord_redirect_uri=os.environ.get("SITE_DISCORD_REDIRECT_URI", ""),
        discord_role_ids=_list("SITE_DISCORD_ROLE_IDS", []),
        image_max_bytes=_int("SITE_IMAGE_MAX_BYTES", 10 * 1024 * 1024),
        file_max_bytes=_int("SITE_FILE_MAX_BYTES", 50 * 1024 * 1024),
        allowed_mimes=_list(
            "SITE_ALLOWED_MIMES",
            [
                "image/png",
                "image/jpeg",
                "image/webp",
                "image/gif",
                "application/json",
                "application/zip",
                "video/mp4",
            ],
        ),
        max_tags=_int("SITE_MAX_TAGS", 15),
    )


def reload_config() -> SiteConfig:
    """清掉缓存重新读环境变量（测试用）。"""
    from site_v1.rate_limit import get_rate_limit_config

    get_config.cache_clear()
    get_rate_limit_config.cache_clear()
    return get_config()
