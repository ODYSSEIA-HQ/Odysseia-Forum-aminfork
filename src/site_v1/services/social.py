"""关注用户（复用 author_follow）、屏蔽用户（site_user_block）。"""

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert

from models.author_follow import AuthorFollow
from models.discord_identity import DiscordIdentity
from models.site_account import SiteUserBlock
from shared.time_utils import utc_now


async def _discord_id(session, user_id: str) -> int | None:
    value = (
        await session.execute(
            select(DiscordIdentity.discord_user_id).where(
                DiscordIdentity.user_id == user_id, DiscordIdentity.is_current.is_(True)
            )
        )
    ).scalar_one_or_none()
    return int(value) if value and value.isdigit() else None


async def is_following(session, follower_id: str, author_id: str) -> bool:
    return (
        await session.execute(
            select(AuthorFollow.id).where(
                AuthorFollow.fluxer_user_id == follower_id,
                AuthorFollow.fluxer_author_id == author_id,
                AuthorFollow.active_flag.is_(True),
            )
        )
    ).first() is not None


async def set_follow(session, follower_id: str, author_id: str, follow: bool) -> None:
    """和 V2 WorkAuthorService.follow_author 写同一份 author_follow 事实。

    V2 那里要求关注者有 Discord 绑定（user_id 写 Discord ID）；协议账号可以没有绑定，
    这里 Discord ID 列有就填、没有就留空，按 fluxer_user_id / fluxer_author_id 定位。
    """
    follower_discord = await _discord_id(session, follower_id)
    author_discord = await _discord_id(session, author_id)
    conditions = [
        and_(
            AuthorFollow.fluxer_user_id == follower_id,
            AuthorFollow.fluxer_author_id == author_id,
        )
    ]
    if follower_discord and author_discord:
        conditions.append(
            and_(
                AuthorFollow.user_id == follower_discord,
                AuthorFollow.author_id == author_discord,
            )
        )
    row = (
        (
            await session.execute(
                select(AuthorFollow).where(or_(*conditions)).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        if follow:
            session.add(
                AuthorFollow(
                    user_id=follower_discord,
                    author_id=author_discord,
                    fluxer_user_id=follower_id,
                    fluxer_author_id=author_id,
                    active_flag=True,
                )
            )
        return
    row.fluxer_user_id = row.fluxer_user_id or follower_id
    row.fluxer_author_id = row.fluxer_author_id or author_id
    if follow and not row.active_flag:
        row.followed_at = utc_now()
    row.active_flag = follow


async def is_blocked(session, user_id: str, blocked_user_id: str) -> bool:
    # 身份头模式：本人的屏蔽名单以网关 Principal.blockedUserIds 为准（本地表也认）
    from site_v1 import principal as principals

    p = principals.for_user(user_id)
    if p is not None and blocked_user_id in p.blocked_user_ids:
        return True
    return (
        await session.execute(
            select(SiteUserBlock.user_id).where(
                SiteUserBlock.user_id == user_id,
                SiteUserBlock.blocked_user_id == blocked_user_id,
            )
        )
    ).first() is not None


async def blocked_user_ids(session, user_id: str) -> set[str]:
    """屏蔽名单（作品列表过滤用）。身份头模式下并上 Principal.blockedUserIds。"""
    from site_v1 import principal as principals

    p = principals.for_user(user_id)
    extra = set(p.blocked_user_ids) if p is not None else set()
    return extra | set(
        (
            await session.execute(
                select(SiteUserBlock.blocked_user_id).where(
                    SiteUserBlock.user_id == user_id
                )
            )
        ).scalars()
    )


async def set_block(session, user_id: str, target_id: str, block: bool) -> None:
    if block:
        await session.execute(
            insert(SiteUserBlock)
            .values(user_id=user_id, blocked_user_id=target_id, created_at=utc_now())
            .on_conflict_do_nothing()
        )
        # 屏蔽后不再关注对方
        await set_follow(session, user_id, target_id, False)
    else:
        await session.execute(
            delete(SiteUserBlock).where(
                SiteUserBlock.user_id == user_id,
                SiteUserBlock.blocked_user_id == target_id,
            )
        )
