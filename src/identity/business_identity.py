"""按声明的协议来源解析统一业务身份。"""

from identity.canonical_user_id import CanonicalUserId
from identity.identity_repository import IdentityRepository
from identity.runtime import request_identity_mode, uses_canonical_storage


async def resolve_business_id(session, user_id, *, source=None):
    """V1 与 Bot 声明 Discord 空间，V2 声明 Fluxer 空间。"""
    if not uses_canonical_storage():
        return user_id
    if isinstance(user_id, CanonicalUserId):
        return user_id
    namespace = source or (
        "fluxer" if request_identity_mode.get() == "fluxer" else "discord"
    )
    if (
        namespace not in ("discord", "fluxer")
        or user_id is None
        or isinstance(user_id, bool)
        or not str(user_id)
    ):
        raise ValueError("缺少有效且已声明来源的用户身份")
    if namespace == "fluxer":
        return CanonicalUserId(str(user_id))
    mappings = await IdentityRepository(session).discord_to_fluxer([user_id])
    resolved = mappings.get(str(user_id))
    if resolved is None:
        raise ValueError(f"Discord 用户 {user_id} 尚未映射到统一账户")
    return CanonicalUserId(resolved, source_discord_id=int(user_id))


async def resolve_business_ids(session, user_ids, *, source=None):
    """同一批输入只允许一个声明来源，批量读取映射避免逐行 RPC。"""
    if not uses_canonical_storage():
        return user_ids
    namespace = source or (
        "fluxer" if request_identity_mode.get() == "fluxer" else "discord"
    )
    if namespace == "fluxer":
        return [CanonicalUserId(str(value)) for value in user_ids]
    originals = [value for value in user_ids if not isinstance(value, CanonicalUserId)]
    mappings = await IdentityRepository(session).discord_to_fluxer(originals)
    result = []
    for value in user_ids:
        if isinstance(value, CanonicalUserId):
            result.append(value)
        elif not isinstance(value, bool) and str(value) in mappings:
            result.append(
                CanonicalUserId(mappings[str(value)], source_discord_id=int(value))
            )
        else:
            raise ValueError(f"Discord 用户 {value} 尚未映射到统一账户")
    return result


async def discord_id_for_business_id(session, user_id):
    """真实 Discord 来源保留原始审计 ID，原生操作才查当前绑定。"""
    if not uses_canonical_storage():
        return user_id
    if isinstance(user_id, CanonicalUserId) and user_id.source_discord_id is not None:
        return user_id.source_discord_id
    mapping = await IdentityRepository(session).fluxer_to_discord([user_id])
    value = mapping.get(str(user_id))
    return int(value) if value else None
