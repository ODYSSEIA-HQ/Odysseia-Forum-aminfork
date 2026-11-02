"""显式启用 Fluxer，旧部署默认保持 Discord 行为。"""

import os
from contextvars import ContextVar

request_identity_mode = ContextVar("request_identity_mode", default=None)


def is_fluxer_mode() -> bool:
    """只接受显式配置，避免部分部署误切身份空间。"""
    override = request_identity_mode.get()
    if override is not None:
        return override == "fluxer"
    mode = os.environ.get("FORUM_AUTH_MODE", "legacy").strip().lower()
    if mode not in {"legacy", "fluxer"}:
        raise RuntimeError("FORUM_AUTH_MODE 必须为 legacy 或 fluxer")
    return mode == "fluxer"


def uses_canonical_storage() -> bool:
    """仅决定数据库身份列，外部 V1/V2 协议由独立入口声明。"""
    return os.environ.get("FORUM_V2_ENABLED", "").lower() == "true" or is_fluxer_mode()
