"""Fluxer 适配层：get_fluxer() 按 FLUXER_FAKE 返回真实或假实现（进程内单例，测试可 set_fluxer 替换）。"""

from site_v1.fluxer.port import (
    AccountExists,
    AccountUnavailable,
    DiscordUser,
    FluxerError,
    FluxerForbidden,
    FluxerIdentity,
    FluxerMessage,
    FluxerNotFound,
    FluxerPort,
    FluxerSession,
    FluxerUnauthorized,
    FluxerUnavailable,
    InvalidCredentials,
    MfaRequired,
    Origin,
    UnreadSummary,
)

_instance: FluxerPort | None = None


def get_fluxer() -> FluxerPort:
    global _instance
    if _instance is None:
        from site_v1.config import get_config

        if get_config().fluxer_fake:
            from site_v1.fluxer.fake import FakeFluxer

            _instance = FakeFluxer()
        else:
            from site_v1.fluxer.real import RealFluxer

            _instance = RealFluxer()
    return _instance


def set_fluxer(port: FluxerPort | None) -> None:
    """替换实现（测试用）；传 None 下次按配置重建。"""
    global _instance
    _instance = port


__all__ = [
    "AccountExists",
    "AccountUnavailable",
    "DiscordUser",
    "FluxerError",
    "FluxerForbidden",
    "FluxerMessage",
    "FluxerNotFound",
    "FluxerUnauthorized",
    "FluxerIdentity",
    "FluxerPort",
    "FluxerSession",
    "FluxerUnavailable",
    "InvalidCredentials",
    "MfaRequired",
    "Origin",
    "UnreadSummary",
    "get_fluxer",
    "set_fluxer",
]
