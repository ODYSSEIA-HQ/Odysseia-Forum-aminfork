"""娱乐账户（类脑币）：GET /fun/wallet、POST /fun/wallet/daily、GET /fun/leaderboard。

照搬假后端 handlers/fun.ts：第一次进娱乐区送 1000；每日领取 200（按北京时间自然日一次）；
排行榜按余额前 20。改余额的地方一律先 lock_wallet（行锁），同一事务里改完。
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from models.site_fun import SiteFunWallet
from models.user import User
from shared.time_utils import utc_now
from site_v1.dto import iso_utc, load_users
from site_v1.errors import conflict

FUN_INITIAL_BALANCE = 1000
FUN_DAILY_AMOUNT = 200


def day_key(at: datetime) -> str:
    """北京时间的日期（YYYY-MM-DD）。"""
    return (at + timedelta(hours=8)).strftime("%Y-%m-%d")


async def lock_wallet(session, user_id: str) -> SiteFunWallet:
    """取钱包并加行锁；没有就建（送初始类脑币）。"""
    now = utc_now()
    await session.execute(
        insert(SiteFunWallet)
        .values(user_id=user_id, balance=FUN_INITIAL_BALANCE, created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=[SiteFunWallet.user_id])
    )
    return (
        await session.execute(
            select(SiteFunWallet).where(SiteFunWallet.user_id == user_id).with_for_update()
        )
    ).scalar_one()


def wallet_dto(w: SiteFunWallet) -> dict:
    return {
        "balance": int(w.balance),
        "lastDailyAt": iso_utc(w.last_daily_at),
        "canClaimDaily": not w.last_daily_at or day_key(w.last_daily_at) != day_key(utc_now()),
        "dailyAmount": FUN_DAILY_AMOUNT,
    }


def credit(w: SiteFunWallet, amount: int) -> None:
    w.balance = int(w.balance) + int(amount)
    w.updated_at = utc_now()


async def get_wallet(session, user_id: str) -> dict:
    return wallet_dto(await lock_wallet(session, user_id))


async def claim_daily(session, user_id: str) -> dict:
    w = await lock_wallet(session, user_id)
    if not wallet_dto(w)["canClaimDaily"]:
        raise conflict(
            "今天已经领过了，明天再来",
            reason="already_claimed",
            lastDailyAt=iso_utc(w.last_daily_at),
        )
    credit(w, FUN_DAILY_AMOUNT)
    w.last_daily_at = utc_now()
    return {"amount": FUN_DAILY_AMOUNT, "wallet": wallet_dto(w)}


async def leaderboard(session, user_id: str) -> list[dict]:
    await lock_wallet(session, user_id)
    rows = (
        await session.execute(
            select(SiteFunWallet.user_id, SiteFunWallet.balance)
            .join(User, User.id == SiteFunWallet.user_id)
            .where(User.merged_into_id.is_(None))
            .order_by(SiteFunWallet.balance.desc(), SiteFunWallet.user_id)
            .limit(20)
        )
    ).all()
    users = await load_users(session, [r[0] for r in rows])
    return [
        {"rank": i + 1, "user": users[uid], "balance": int(balance)}
        for i, (uid, balance) in enumerate(r for r in rows if r[0] in users)
    ]
