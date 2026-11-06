"""实时推送（Centrifugo）的配置：全部来自环境变量，读一次缓存在进程里。

- CENTRIFUGO_TOKEN_SECRET + CENTRIFUGO_PUBLIC_URL：都配了才签发连接令牌、订阅令牌
  （GET /realtime/token、/realtime/subscribe-token）；缺一个这两个接口返回 503。
- CENTRIFUGO_API_URL + CENTRIFUGO_API_KEY：都配了才调 Centrifugo 的 HTTP server API 发布；
  缺一个发布是空操作（ORM 钩子也不收集变化）。
测试改环境变量后调用 reload_realtime_config()。
"""

import os
from dataclasses import dataclass
from functools import lru_cache


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值 {raw!r}") from exc
    return max(minimum, value)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是数字，当前值 {raw!r}") from exc


@dataclass(frozen=True)
class RealtimeConfig:
    api_url: str
    """Centrifugo HTTP API 根地址（服务端到服务端），例如 http://centrifugo:8000；发布打 {api_url}/api/publish。"""

    api_key: str
    """Centrifugo http_api.key（v5：api_key），放在 X-API-Key 头。"""

    token_secret: str
    """连接令牌、订阅令牌的 HMAC（HS256）密钥，和 Centrifugo client.token.hmac_secret_key 相同。"""

    public_url: str
    """浏览器连的 WebSocket 地址，例如 wss://rt.example.com/connection/websocket（放进 /realtime/token 响应）。"""

    token_ttl: int = 600
    """连接令牌有效期（秒）。到期前 centrifuge-js 自己调 /realtime/token 续。"""

    sub_token_ttl: int = 300
    """订阅令牌有效期（秒）。到期续签时重新按可见性校验，是权限变化的兜底。"""

    publish_timeout: float = 3.0

    @property
    def tokens_enabled(self) -> bool:
        return bool(self.token_secret and self.public_url)

    @property
    def publish_enabled(self) -> bool:
        return bool(self.api_url and self.api_key)


@lru_cache(maxsize=1)
def realtime_config() -> RealtimeConfig:
    return RealtimeConfig(
        api_url=os.environ.get("CENTRIFUGO_API_URL", "").strip().rstrip("/"),
        api_key=os.environ.get("CENTRIFUGO_API_KEY", "").strip(),
        token_secret=os.environ.get("CENTRIFUGO_TOKEN_SECRET", "").strip(),
        public_url=os.environ.get("CENTRIFUGO_PUBLIC_URL", "").strip(),
        token_ttl=_int("CENTRIFUGO_TOKEN_TTL", 600, 30),
        sub_token_ttl=_int("CENTRIFUGO_SUB_TOKEN_TTL", 300, 30),
        publish_timeout=_float("CENTRIFUGO_PUBLISH_TIMEOUT", 3.0),
    )


def reload_realtime_config() -> RealtimeConfig:
    realtime_config.cache_clear()
    return realtime_config()


def publishing_enabled() -> bool:
    return realtime_config().publish_enabled
