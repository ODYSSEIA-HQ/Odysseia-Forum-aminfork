"""/api/v1 限频（协议 1.6）：写接口按账号限频，未登录接口按 IP 限频。

超限返回 429 rate_limited，带 Retry-After（秒，等于当前窗口还剩多久）和 X-Content-Mode。
用 Redis 固定窗口计数（INCR + EXPIRE，一段 Lua 保证原子），键都在 site:rl:* 下
（契约测试脚本清 site:* 时一起清掉）。旧接口的 RateLimitMiddleware 只管 /v1、/v2，和这里无关。

三个桶（阈值都可以用环境变量改，见 docs/site_v1/README.md「限频」）：
- account：已登录账号的写请求（POST/PUT/PATCH/DELETE），默认 240 次 / 60 秒；
- ip_write：未登录请求里的写请求（登录、注册等），按 IP，默认 60 次 / 60 秒；
- ip：未登录的全部请求（含读），按 IP，默认 600 次 / 60 秒。

IP 只信任 CF-Connecting-IP（SITE_RL_TRUST_CF=1 时），或受信代理链最右边的不可信地址
（直连地址在 SITE_RL_TRUSTED_PROXIES 里时，从右往左跳过受信代理）；从不取 X-Forwarded-For 第一个值。
Redis 出错时放行（只记日志）：会话读取那一步已经会在 Redis 不可用时返回 503。
"""

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

from site_v1.errors import SiteError

logger = logging.getLogger(__name__)

KEY_PREFIX = "site:rl:"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 返回 {当前计数, 剩余秒数}；第一次计数时设过期，过期丢了（极少见）时补上
_SCRIPT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {n, ttl}
"""


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值 {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Bucket:
    name: str
    limit: int
    window: int

    @property
    def active(self) -> bool:
        return self.limit > 0 and self.window > 0


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool
    account: Bucket
    ip_write: Bucket
    ip: Bucket
    trust_cf: bool
    trusted_proxies: list = field(default_factory=list)


@lru_cache(maxsize=1)
def get_rate_limit_config() -> RateLimitConfig:
    proxies = []
    for part in (os.environ.get("SITE_RL_TRUSTED_PROXIES") or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            proxies.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise RuntimeError(
                f"SITE_RL_TRUSTED_PROXIES 里 {part!r} 不是 IP 或网段"
            ) from exc
    return RateLimitConfig(
        enabled=_bool("SITE_RL_ENABLED", True),
        account=Bucket(
            "account",
            _int("SITE_RL_ACCOUNT_WRITE_LIMIT", 240),
            _int("SITE_RL_ACCOUNT_WRITE_WINDOW", 60),
        ),
        ip_write=Bucket(
            "ip_write",
            _int("SITE_RL_IP_WRITE_LIMIT", 60),
            _int("SITE_RL_IP_WRITE_WINDOW", 60),
        ),
        ip=Bucket("ip", _int("SITE_RL_IP_LIMIT", 600), _int("SITE_RL_IP_WINDOW", 60)),
        trust_cf=_bool("SITE_RL_TRUST_CF", False),
        trusted_proxies=proxies,
    )


def _parse_ip(raw: str | None):
    if not raw:
        return None
    raw = raw.strip()
    # 去掉 [v6]:port、v4:port
    if raw.startswith("[") and "]" in raw:
        raw = raw[1 : raw.index("]")]
    elif raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def _trusted(ip, config: RateLimitConfig) -> bool:
    return any(ip in net for net in config.trusted_proxies)


def client_ip(scope, headers, config: RateLimitConfig | None = None) -> str:
    """按协议 1.6 取客户端 IP。headers 是 starlette Headers（大小写不敏感）。"""
    config = config or get_rate_limit_config()
    if config.trust_cf:
        cf = _parse_ip(headers.get("cf-connecting-ip"))
        if cf is not None:
            return str(cf)
    peer_raw = (scope.get("client") or ("", 0))[0]
    peer = _parse_ip(peer_raw)
    if peer is not None and config.trusted_proxies and _trusted(peer, config):
        chain = [
            p for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()
        ]
        for raw in reversed(chain):
            ip = _parse_ip(raw)
            if ip is None:
                break
            if not _trusted(ip, config):
                return str(ip)
    return str(peer) if peer is not None else (peer_raw or "unknown")


async def _hit(redis, bucket: Bucket, ident: str) -> int | None:
    """计一次；超限返回 Retry-After 秒数，没超返回 None。"""
    n, ttl = await redis.eval(
        _SCRIPT, 1, f"{KEY_PREFIX}{bucket.name}:{ident}", bucket.window
    )
    if int(n) > bucket.limit:
        return max(1, int(ttl))
    return None


def rate_limited(scope_name: str, retry_after: int) -> SiteError:
    return SiteError(
        429,
        "rate_limited",
        "操作太频繁，请稍后再试",
        {"scope": scope_name, "retryAfter": retry_after},
        retry_after=retry_after,
    )


async def check(scope, headers, method: str, user_id: str | None) -> None:
    """按请求计数；超限抛 SiteError(429)。"""
    config = get_rate_limit_config()
    if not config.enabled:
        return
    from site_v1.deps import get_redis

    method = method.upper()
    write = method in WRITE_METHODS
    if user_id:
        plan = [(config.account, f"u:{user_id}")] if write else []
        scope_name = "account"
    else:
        ip = client_ip(scope, headers, config)
        plan = [(config.ip, ip)]
        if write:
            plan.append((config.ip_write, ip))
        scope_name = "ip"
    plan = [(b, i) for b, i in plan if b.active]
    if not plan:
        return
    try:
        redis = get_redis()
        for bucket, ident in plan:
            retry = await _hit(redis, bucket, ident)
            if retry is not None:
                raise rate_limited(scope_name, retry)
    except SiteError:
        raise
    except Exception:
        logger.warning("site_v1 限频计数失败，放行", exc_info=True)


async def reset(redis) -> int:
    """清掉全部限频计数（测试钩子、测试用）。"""
    keys = [k async for k in redis.scan_iter(f"{KEY_PREFIX}*")]
    if keys:
        await redis.delete(*keys)
    return len(keys)
