"""账号服务：懒建站内账号行、读 Viewer、偏好、R18 资格条件。"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from models.discord_identity import DiscordIdentity
from models.site_account import SiteAccount, SiteRestrictedAccess, SiteUserPrefs
from models.user import User
from shared.time_utils import utc_now
from site_v1.config import SiteConfig, get_config
from site_v1.context import MEMBER_PERMISSIONS, Prefs, RestrictedAccess, Viewer

CONDITION_LABELS = {
    "age_confirm": "确认已满 18 岁并同意当前条款",
    "discord_role": "绑定 Discord 并在类脑社区有指定身份组",
}


async def ensure_account_rows(
    session,
    user_id: str,
    *,
    email: str | None = None,
    newcomer: bool = False,
    config: SiteConfig | None = None,
) -> None:
    """第一次经 /api/v1 访问时建 site_account / prefs / restricted_access（幂等）。

    newcomer=True（站内注册）时设新人缓冲期；V2 已有账号首次访问不算新人。
    """
    config = config or get_config()
    now = utc_now()
    await session.execute(
        insert(SiteAccount)
        .values(
            user_id=user_id,
            email=email,
            created_at=now,
            newcomer_until=now + timedelta(days=config.newcomer_days)
            if newcomer
            else None,
            quiz_passed=False,
            permissions=[],
        )
        .on_conflict_do_nothing(index_elements=[SiteAccount.user_id])
    )
    await session.execute(
        insert(SiteUserPrefs)
        .values(
            user_id=user_id,
            content_mode="general",
            show_r18g=False,
            blur_r18=True,
            blocked_tag_ids=[],
            followed_tag_ids=[],
            dm_privacy="everyone",
            pinned_boards=[],
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[SiteUserPrefs.user_id])
    )
    await session.execute(
        insert(SiteRestrictedAccess)
        .values(user_id=user_id, state="none", updated_at=now)
        .on_conflict_do_nothing(index_elements=[SiteRestrictedAccess.user_id])
    )


def prefs_of(row: SiteUserPrefs) -> Prefs:
    return Prefs(
        content_mode=row.content_mode,
        show_r18g=row.show_r18g,
        blur_r18=row.blur_r18,
        blocked_tags=list(row.blocked_tag_ids or []),
        followed_tags=list(row.followed_tag_ids or []),
        dm_privacy=row.dm_privacy,
        pinned_boards=list(row.pinned_boards or []),
    )


def access_of(row: SiteRestrictedAccess) -> RestrictedAccess:
    return RestrictedAccess(
        state=row.state,
        source=row.source,
        checked_at=row.checked_at,
        age_confirmed_at=row.age_confirmed_at,
        terms_version=row.terms_version,
    )


async def load_viewer(session_factory, user_id: str) -> Viewer | None:
    """读当前账号的完整快照；账号不存在或已合并返回 None（按未登录处理）。"""
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None or user.merged_into_id:
            return None
        account = await session.get(SiteAccount, user_id)
        prefs = await session.get(SiteUserPrefs, user_id)
        access = await session.get(SiteRestrictedAccess, user_id)
        if account is None or prefs is None or access is None:
            await ensure_account_rows(session, user_id)
            await session.commit()
            account = await session.get(SiteAccount, user_id)
            prefs = await session.get(SiteUserPrefs, user_id)
            access = await session.get(SiteRestrictedAccess, user_id)
        discord_id = (
            await session.execute(
                select(DiscordIdentity.discord_user_id).where(
                    DiscordIdentity.user_id == user_id,
                    DiscordIdentity.is_current.is_(True),
                )
            )
        ).scalar_one_or_none()
        return Viewer(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            discord_user_id=discord_id,
            email=account.email,
            created_at=account.created_at,
            newcomer_until=account.newcomer_until,
            quiz_passed=account.quiz_passed,
            permissions=frozenset(MEMBER_PERMISSIONS)
            | frozenset(account.permissions or []),
            prefs=prefs_of(prefs),
            access=access_of(access),
        )


# ---------------- R18 资格 ----------------


async def discord_role_met(viewer: Viewer) -> bool:
    """「绑定 Discord 并有指定身份组」。fake 模式只看是否绑定；真实环境查成员身份组。"""
    if not viewer.discord_linked:
        return False
    config = get_config()
    if config.fluxer_fake:
        return True
    from site_v1.membership import has_member_role

    return await has_member_role(viewer)


async def access_conditions(
    viewer: Viewer, config: SiteConfig | None = None
) -> list[dict]:
    """协议 RestrictedAccessStatus.conditions：[{id, label, met}]，条件由配置决定。"""
    config = config or get_config()
    result = []
    for cid in config.restricted_access_conditions:
        if cid == "age_confirm":
            met = (
                viewer.access.age_confirmed_at is not None
                and viewer.access.terms_version == config.terms_version
            )
        elif cid == "discord_role":
            met = await discord_role_met(viewer)
        else:
            continue
        result.append({"id": cid, "label": CONDITION_LABELS.get(cid, cid), "met": met})
    return result


def unmet_ids(viewer: Viewer, conditions: list[dict]) -> list[str]:
    unmet = [c["id"] for c in conditions if not c["met"]]
    if viewer.access.state == "revoked":
        return ["revoked", *unmet]
    if viewer.access.state != "granted" and not unmet:
        # 条件都满足但还没授予（例如还没确认）：前端去确认页
        return ["age_confirm"] if "age_confirm" not in unmet else unmet
    return unmet
