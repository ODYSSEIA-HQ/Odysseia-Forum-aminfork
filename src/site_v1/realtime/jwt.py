"""Centrifugo 用的 HMAC JWT（HS256）：签发和校验（校验给测试、排查用）。

不引第三方库：头固定 {"alg":"HS256","typ":"JWT"}，base64url 不带填充。
Centrifugo 认的声明：sub（用户 ID，匿名为空串）、exp、iat；
连接令牌 channels（服务端订阅的频道）；订阅令牌 channel（只对这一个频道有效）。
"""

import base64
import hashlib
import hmac
import json
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode(claims: dict, secret: str) -> str:
    header = _b64(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    payload = _b64(
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


class InvalidToken(ValueError):
    pass


def decode(token: str, secret: str, *, now: float | None = None) -> dict:
    """校验签名、alg 和 exp，返回声明；不对抛 InvalidToken。"""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_unb64(header_b64))
        claims = json.loads(_unb64(payload_b64))
        signature = _unb64(sig_b64)
    except (ValueError, TypeError) as exc:
        raise InvalidToken("格式不对") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidToken("alg 不是 HS256")
    expected = hmac.new(
        secret.encode(), f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidToken("签名不对")
    if not isinstance(claims, dict):
        raise InvalidToken("声明不是对象")
    exp = claims.get("exp")
    if exp is not None and (now if now is not None else time.time()) >= float(exp):
        raise InvalidToken("已过期")
    return claims
