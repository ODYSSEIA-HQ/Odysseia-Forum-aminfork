"""21 点（照搬 web 仓库 src/mock/blackjack.ts 的规则和 handlers/fun.ts 的接口行为）。

规则：六副牌的牌靴、庄家 17 点停牌（软 17 也停）、黑杰克赔 3:2（向下取整）、平局退注；
只能在头两张牌时加倍（加倍后只再要一张）；不做分牌和保险。开局任一方黑杰克直接结算。

防作弊：牌靴在服务端（site_fun_bj_shoe，系统随机数洗牌），只下发看得见的牌，庄家暗牌结算前不下发；
所有改余额的操作先锁钱包行，同一个人的开局、动作串行。
"""

import secrets

from sqlalchemy import delete, select

from models.site_fun import SiteFunBlackjackRound, SiteFunBlackjackShoe
from shared.time_utils import utc_now
from site_v1.dto import iso_utc
from site_v1.errors import SiteError, conflict, invalid, not_found
from site_v1.services.fun.wallet import credit, lock_wallet
from site_v1.services.work_access import parse_id

SUITS = ("spades", "hearts", "diamonds", "clubs")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SHOE_DECKS = 6
RESHUFFLE_RATIO = 0.25
MIN_BET = 10
MAX_BET = 5000
KEEP_ROUNDS = 50

_rng = secrets.SystemRandom()


# ---------------- 纯规则 ----------------


def card_points(card: dict) -> int:
    rank = card["rank"]
    if rank == "A":
        return 1
    if rank in ("J", "Q", "K"):
        return 10
    return int(rank)


def hand_value(cards: list[dict]) -> tuple[int, bool]:
    total = sum(card_points(c) for c in cards)
    aces = sum(1 for c in cards if c["rank"] == "A")
    soft = aces > 0 and total + 10 <= 21
    return (total + 10 if soft else total), soft


def is_blackjack(cards: list[dict]) -> bool:
    return len(cards) == 2 and hand_value(cards)[0] == 21


def dealer_should_hit(cards: list[dict]) -> bool:
    return hand_value(cards)[0] < 17


def create_shoe(decks: int = SHOE_DECKS, rng=None) -> list[dict]:
    rng = rng or _rng
    cards = [{"rank": r, "suit": s} for _ in range(decks) for s in SUITS for r in RANKS]
    rng.shuffle(cards)
    return cards


def decide_outcome(player: list[dict], dealer: list[dict]) -> str:
    p = hand_value(player)[0]
    if p > 21:
        return "bust"
    p_bj, d_bj = is_blackjack(player), is_blackjack(dealer)
    if p_bj and d_bj:
        return "push"
    if p_bj:
        return "blackjack"
    if d_bj:
        return "lose"
    d = hand_value(dealer)[0]
    if d > 21:
        return "dealer_bust"
    if p > d:
        return "win"
    if p < d:
        return "lose"
    return "push"


def payout_for(outcome: str, bet: int) -> int:
    if outcome == "blackjack":
        return bet + (bet * 3) // 2
    if outcome in ("win", "dealer_bust"):
        return bet * 2
    if outcome == "push":
        return bet
    return 0


def _settle(core: dict, dealer_plays: bool, draw) -> dict:
    dealer = list(core["dealer"])
    if dealer_plays:
        while dealer_should_hit(dealer):
            dealer.append(draw())
    outcome = decide_outcome(core["player"], dealer)
    return {**core, "dealer": dealer, "state": "settled", "outcome": outcome, "payout": payout_for(outcome, core["bet"])}


def start_round(bet: int, draw) -> dict:
    p1, d1, p2, d2 = draw(), draw(), draw(), draw()
    core = {"bet": bet, "doubled": False, "player": [p1, p2], "dealer": [d1, d2], "state": "playing", "outcome": None, "payout": 0}
    if is_blackjack(core["player"]) or is_blackjack(core["dealer"]):
        return _settle(core, False, draw)
    return core


def available_actions(core: dict, can_afford_double: bool) -> list[str]:
    if core["state"] != "playing":
        return []
    actions = ["hit", "stand"]
    if len(core["player"]) == 2 and can_afford_double:
        actions.append("double")
    return actions


def apply_action(core: dict, action: str, draw) -> dict:
    if action == "stand":
        return _settle(core, True, draw)
    player = [*core["player"], draw()]
    nxt = {**core, "player": player}
    if action == "double":
        nxt.update(bet=core["bet"] * 2, doubled=True)
    total = hand_value(player)[0]
    if total > 21:
        return _settle(nxt, False, draw)
    if action == "double" or total == 21:
        return _settle(nxt, True, draw)
    return nxt


# ---------------- 存取 ----------------


def _core(r: SiteFunBlackjackRound) -> dict:
    return {
        "bet": int(r.bet), "doubled": r.doubled, "player": list(r.player), "dealer": list(r.dealer),
        "state": r.state, "outcome": r.outcome, "payout": int(r.payout),
    }


def _hand(cards: list[dict], hide_hole: bool) -> dict:
    visible = cards[:1] if hide_hole else cards
    total, soft = hand_value(visible)
    return {"cards": visible, "total": total, "soft": soft, "hidden": len(cards) - len(visible)}


def round_dto(r: SiteFunBlackjackRound, balance: int) -> dict:
    settled = r.state == "settled"
    core = _core(r)
    return {
        "id": str(r.id),
        "bet": int(r.bet),
        "doubled": r.doubled,
        "state": r.state,
        "player": _hand(core["player"], False),
        # 结算前庄家暗牌不下发
        "dealer": _hand(core["dealer"], not settled),
        "actions": available_actions(core, balance >= int(r.bet)),
        "outcome": r.outcome,
        "payout": int(r.payout),
        "net": int(r.payout) - int(r.bet) if settled else 0,
        "balance": balance,
        "createdAt": iso_utc(r.created_at),
        "settledAt": iso_utc(r.settled_at),
    }


class Shoe:
    """这个账号的牌靴：从前往后发；空了就新洗一靴（正常不会发生，开局前已检查）。"""

    def __init__(self, row: SiteFunBlackjackShoe):
        self.row = row
        self.cards = list(row.cards)

    def draw(self) -> dict:
        if not self.cards:
            self.cards = create_shoe()
            self.row.size = len(self.cards)
        return self.cards.pop(0)

    def save(self) -> None:
        self.row.cards = self.cards


async def _shoe(session, user_id: str) -> SiteFunBlackjackShoe:
    row = await session.get(SiteFunBlackjackShoe, user_id, with_for_update=True)
    if row is None:
        cards = create_shoe()
        row = SiteFunBlackjackShoe(user_id=user_id, cards=cards, size=len(cards))
        session.add(row)
    return row


async def _active(session, user_id: str) -> SiteFunBlackjackRound | None:
    return (
        await session.execute(
            select(SiteFunBlackjackRound)
            .where(SiteFunBlackjackRound.user_id == user_id, SiteFunBlackjackRound.state == "playing")
            .order_by(SiteFunBlackjackRound.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _commit(r: SiteFunBlackjackRound, core: dict, wallet) -> None:
    """写回局面；刚结算的把退回的类脑币加回钱包。"""
    r.bet, r.doubled, r.player, r.dealer = core["bet"], core["doubled"], core["player"], core["dealer"]
    r.state, r.outcome, r.payout = core["state"], core["outcome"], core["payout"]
    if r.state == "settled" and r.settled_at is None:
        r.settled_at = utc_now()
        credit(wallet, r.payout)


async def current_round(session, user_id: str) -> dict:
    w = await lock_wallet(session, user_id)
    r = await _active(session, user_id)
    return {"round": round_dto(r, int(w.balance)) if r else None}


async def start(session, user_id: str, body: dict) -> dict:
    bet = body.get("bet")
    if isinstance(bet, bool) or not isinstance(bet, int):
        if isinstance(bet, float) and bet.is_integer():
            bet = int(bet)
        else:
            raise invalid({"bet": "下注要是整数"})
    w = await lock_wallet(session, user_id)
    if bet < MIN_BET:
        raise invalid({"bet": f"最少下注 {MIN_BET}"})
    if bet > MAX_BET:
        raise invalid({"bet": f"单局最多下注 {MAX_BET}"})
    if bet > int(w.balance):
        raise invalid({"bet": "类脑币不够"}, "类脑币不够")
    current = await _active(session, user_id)
    if current is not None:
        raise conflict("上一局还没打完", reason="round_in_progress", roundId=str(current.id))

    shoe_row = await _shoe(session, user_id)
    # 牌靴剩得少了先重洗
    if len(shoe_row.cards) < shoe_row.size * RESHUFFLE_RATIO:
        shoe_row.cards = create_shoe()
        shoe_row.size = len(shoe_row.cards)
    shoe = Shoe(shoe_row)

    credit(w, -bet)
    core = start_round(bet, shoe.draw)
    r = SiteFunBlackjackRound(user_id=user_id, bet=bet, doubled=False, player=[], dealer=[], state="playing", payout=0)
    session.add(r)
    _commit(r, core, w)
    shoe.save()
    await session.flush()
    # 只留最近 50 局
    keep = (
        select(SiteFunBlackjackRound.id)
        .where(SiteFunBlackjackRound.user_id == user_id)
        .order_by(SiteFunBlackjackRound.id.desc())
        .limit(KEEP_ROUNDS)
    )
    await session.execute(
        delete(SiteFunBlackjackRound).where(
            SiteFunBlackjackRound.user_id == user_id,
            SiteFunBlackjackRound.id.not_in(keep.scalar_subquery()),
        )
    )
    return round_dto(r, int(w.balance))


async def act(session, user_id: str, raw_id, body: dict) -> dict:
    w = await lock_wallet(session, user_id)
    round_id = parse_id(raw_id)
    r = await session.get(SiteFunBlackjackRound, round_id, with_for_update=True) if round_id else None
    if r is None or r.user_id != user_id:
        raise not_found()
    action = body.get("action")
    if action not in ("hit", "stand", "double"):
        raise invalid({"action": "不认识的动作"})
    if r.state != "playing":
        raise conflict("这一局已经结束了", reason="round_settled")
    core = _core(r)
    if action not in available_actions(core, int(w.balance) >= int(r.bet)):
        raise SiteError(
            409, "conflict", "现在不能加倍" if action == "double" else "现在不能这样操作",
            {"reason": "action_unavailable"},
        )
    if action == "double":
        credit(w, -int(r.bet))
    shoe = Shoe(await _shoe(session, user_id))
    _commit(r, apply_action(core, action, shoe.draw), w)
    shoe.save()
    return round_dto(r, int(w.balance))
