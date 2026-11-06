"""好友和好友申请（协议 3.8，写法按假后端 handlers/friends.ts）。"""

from sqlalchemy import and_, delete, or_, select

from models.site_me import SiteFriend, SiteFriendRequest


def pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


async def are_friends(session, a: str, b: str) -> bool:
    x, y = pair(a, b)
    return await session.get(SiteFriend, (x, y)) is not None


async def friend_ids(session, user_id: str) -> set[str]:
    rows = (
        await session.execute(
            select(SiteFriend.user_a, SiteFriend.user_b).where(
                or_(SiteFriend.user_a == user_id, SiteFriend.user_b == user_id)
            )
        )
    ).all()
    return {b if a == user_id else a for a, b in rows}


async def request_between(session, a: str, b: str) -> SiteFriendRequest | None:
    return (
        (
            await session.execute(
                select(SiteFriendRequest).where(
                    or_(
                        and_(
                            SiteFriendRequest.from_id == a, SiteFriendRequest.to_id == b
                        ),
                        and_(
                            SiteFriendRequest.from_id == b, SiteFriendRequest.to_id == a
                        ),
                    )
                )
            )
        )
        .scalars()
        .first()
    )


async def make_friends(session, a: str, b: str) -> SiteFriend:
    x, y = pair(a, b)
    row = await session.get(SiteFriend, (x, y))
    if row is None:
        row = SiteFriend(user_a=x, user_b=y)
        session.add(row)
    await session.execute(
        delete(SiteFriendRequest).where(
            or_(
                and_(SiteFriendRequest.from_id == a, SiteFriendRequest.to_id == b),
                and_(SiteFriendRequest.from_id == b, SiteFriendRequest.to_id == a),
            )
        )
    )
    await session.flush()
    return row


async def remove_friend(session, a: str, b: str) -> None:
    x, y = pair(a, b)
    await session.execute(
        delete(SiteFriend).where(SiteFriend.user_a == x, SiteFriend.user_b == y)
    )
