"""账户身份与 Discord 权限的显式边界。"""

from fastapi import HTTPException
from identity.runtime import is_fluxer_mode
from identity.canonical_user_id import CanonicalUserId


def require_discord_id(principal) -> str:
    """Discord 专属操作只允许经过权威绑定投影的 Discord ID。"""
    discord_id = (
        principal.get("discord_user_id") if is_fluxer_mode() else principal.get("id")
    )
    if not discord_id:
        raise HTTPException(status_code=403, detail="此操作需要绑定 Discord 账户")
    return str(discord_id)


def current_identity_id(principal):
    """Fluxer 主体保持字符串，旧模式保留原有数值身份。"""
    if principal.get("canonical_user_id"):
        return CanonicalUserId(
            str(principal["canonical_user_id"]), source_discord_id=int(principal["id"])
        )
    return (
        CanonicalUserId(str(principal["id"]))
        if is_fluxer_mode()
        else int(principal["id"])
    )
