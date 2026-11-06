"""模拟股市（长期常驻盘）：照搬假后端 handlers/stocks.ts 的接口行为，价格模型见 stock_model.py。

**行情推进：按请求时间惰性推进**（不起后台任务）。
- 全站一行状态 site_fun_market(id=1)。读接口先不加锁读；距上次推进不到 1 秒直接用，
  否则 SELECT ... FOR UPDATE 后再推进到现在、写回（多进程、多实例都只靠这把行锁串行，不会重复推进）。
- 下单一律锁行：先推进到现在，再按服务端当前报价校验、成交、记冲击，和余额、持仓、流水同一个事务。
- 没人访问时不跑；下次访问补跑（最多补 7 天，和假后端一样），分红、换日、K 线在补跑里照常发生。
  选惰性推进的原因：不需要常驻进程，多 worker 部署不用选主；代价是首个请求要补跑（最多 5000 步）。
- 第一次访问（或种子脚本）从 60 天前开张，预跑出历史 K 线和新闻（步长 5 分钟）。
- 收好的日 K、阶段 K 写 site_fun_stock_bar；新闻写 site_fun_stock_news（只留最近 500 条）。
- 分红：每天北京时间 20 点，按持股发到钱包（零头攒在 dividend_carry），股价同步除息。
- 新闻：没有 LLM 资讯任务；SITE_FUN_SAMPLE_NEWS=1（默认）时整点按 12% 概率出一条「示例资讯」；
  传闻平均 3 小时一条。不加假后端的「背景成交量」，成交量就是全站玩家的真实成交。
"""

import os
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm.attributes import flag_modified

from models.site_fun import (
    SiteFunMarket,
    SiteFunStockAccount,
    SiteFunStockBar,
    SiteFunStockNews,
    SiteFunStockPosition,
    SiteFunStockTrade,
    SiteFunWallet,
)
from models.user import User
from shared.time_utils import utc_now
from site_v1.dto import iso_utc, load_users
from site_v1.errors import SiteError, invalid, not_found
from site_v1.services.fun import stock_model as sm
from site_v1.services.fun import stock_news as sn
from site_v1.services.fun.stock_companies import COMPANY_BY_SYMBOL, STOCK_COMPANIES
from site_v1.services.fun.wallet import credit, lock_wallet

MARKET_ID = 1
SEED = 20261029
HISTORY_DAYS = 60
BACKFILL_STEP_MS = 5 * 60_000
MAX_NEWS = 500
HOUR_MS = 3600_000
SPARK_STEP_MS = 30 * 60_000
FRESH_MS = 1_000
STOCK_PHASE_NAMES = ("凌晨", "清晨", "上午", "下午", "傍晚", "夜间")
_INIT_LOCK_KEY = 0x5F756E4D  # pg_advisory_xact_lock：开张只做一次


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_dt(t: int) -> datetime:
    return datetime.fromtimestamp(t / 1000, timezone.utc).replace(tzinfo=None)


def iso_ms(t: int) -> str:
    return iso_utc(ms_to_dt(t))


def sample_news_enabled() -> bool:
    return os.environ.get("SITE_FUN_SAMPLE_NEWS", "1") != "0"


class _Collector(sm.Hooks):
    """推进时收集要写库的东西：新闻、分红、收好的 K 线。"""

    def __init__(self):
        self.news: list[dict] = []
        self.dividends: list[tuple[int, dict, float]] = []
        self.bars: list[tuple[str, str, dict]] = []

    def before_step(self, m, t, ms):
        hour = (t + ms) // HOUR_MS * HOUR_MS
        if hour > t and sample_news_enabled() and sm.next_random(m) < sn.SAMPLE_NEWS_CHANCE:
            n = sn.pick_sample_news(m)
            sn.apply_news(m, n)
            self.news.append({**n, "publishedAt": hour, "kind": "news", "sourceName": "示例资讯", "sourceUrl": None})
        if sn.rumor_happens(m, ms):
            r = sn.roll_rumor(m)
            self.news.append({**r, "publishedAt": t, "kind": "rumor", "sourceName": None, "sourceUrl": None})

    def on_dividend(self, m, at, per_share):
        avg_yield = sum(per_share[s["symbol"]] / s["series"]["last"] for s in m["stocks"]) / len(m["stocks"])
        self.dividends.append((at, dict(per_share), avg_yield))

    def on_bar(self, symbol, kind, bar):
        self.bars.append((symbol, kind, bar))


# ---------------- 状态读写 ----------------


async def _flush_collector(session, c: _Collector) -> None:
    if c.bars:
        rows = [
            {
                "symbol": sym, "kind": kind, "start_ms": int(b["start"]), "open": b["open"],
                "high": b["high"], "low": b["low"], "close": b["close"], "volume": int(b["volume"]),
            }
            for sym, kind, b in c.bars
        ]
        for i in range(0, len(rows), 1000):
            stmt = insert(SiteFunStockBar).values(rows[i : i + 1000])
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["symbol", "kind", "start_ms"],
                    set_={k: stmt.excluded[k] for k in ("open", "high", "low", "close", "volume")},
                )
            )
    # 分红：按持股发到钱包（锁钱包行，顺序：市场行 → 钱包行）
    for at, per_share, avg_yield in c.dividends:
        positions = (
            await session.execute(select(SiteFunStockPosition).where(SiteFunStockPosition.qty > 0))
        ).scalars().all()
        holders: dict[str, float] = {}
        for p in positions:
            holders[p.user_id] = holders.get(p.user_id, 0.0) + p.qty * per_share.get(p.symbol, 0.0)
        paid_total = 0
        for user_id in sorted(holders):
            acc = await _lock_account(session, user_id)
            amount = acc.dividend_carry + holders[user_id]
            paid = int(amount // 1)
            acc.dividend_carry = amount - paid
            if paid > 0:
                w = await lock_wallet(session, user_id)
                credit(w, paid)
                acc.dividends = int(acc.dividends) + paid
                paid_total += paid
        c.news.append(
            {
                "publishedAt": at, "kind": "dividend", "scope": "market", "symbols": [], "sector": None,
                "direction": "neutral", "strength": 0,
                "headline": f"今日分红已发放，平均每股派息约 {avg_yield * 100:.3f}%",
                "summary": "按持股数发到娱乐账户，股价同步除息。"
                + (f"本次共发放 {paid_total} 类脑币。" if paid_total > 0 else ""),
                "sourceName": None, "sourceUrl": None,
            }
        )
    if c.news:
        for n in c.news:
            session.add(
                SiteFunStockNews(
                    published_at=ms_to_dt(n["publishedAt"]), kind=n["kind"], scope=n["scope"],
                    symbols=list(n["symbols"]), sector=n["sector"], direction=n["direction"],
                    strength=n["strength"], headline=n["headline"], summary=n["summary"],
                    source_name=n["sourceName"], source_url=n["sourceUrl"],
                )
            )
        await session.flush()
        keep = select(SiteFunStockNews.id).order_by(SiteFunStockNews.published_at.desc(), SiteFunStockNews.id.desc()).limit(MAX_NEWS)
        await session.execute(delete(SiteFunStockNews).where(SiteFunStockNews.id.not_in(keep.scalar_subquery())))
    if c.bars:
        m_phase = max(int(b["start"]) for _, k, b in c.bars if k == "phase") if any(k == "phase" for _, k, _ in c.bars) else None
        m_daily = max(int(b["start"]) for _, k, b in c.bars if k == "daily") if any(k == "daily" for _, k, _ in c.bars) else None
        if m_phase is not None:
            await session.execute(
                delete(SiteFunStockBar).where(
                    SiteFunStockBar.kind == "phase",
                    SiteFunStockBar.start_ms <= m_phase - sm.MAX_PHASE_BARS * sm.GAME_DAY_MS,
                )
            )
        if m_daily is not None:
            await session.execute(
                delete(SiteFunStockBar).where(
                    SiteFunStockBar.kind == "daily",
                    SiteFunStockBar.start_ms <= m_daily - sm.MAX_DAILY_BARS * sm.DAY_MS,
                )
            )


async def _create_market(session) -> SiteFunMarket:
    """从 60 天前开张，一天一天预跑出历史。调用前已拿到开张锁。"""
    t = now_ms()
    start = (t - HISTORY_DAYS * sm.DAY_MS) // sm.GAME_DAY_MS * sm.GAME_DAY_MS
    m = sm.create_market(start, SEED)
    c = _Collector()
    # 一天一天推进（一次推进超过 7 天会被当成「离开太久」直接跳过）
    x = start + sm.DAY_MS
    while x < t:
        sm.advance_to(m, x, c, BACKFILL_STEP_MS)
        x += sm.DAY_MS
    sm.advance_to(m, t, c, BACKFILL_STEP_MS)
    await _flush_collector(session, c)
    row = SiteFunMarket(id=MARKET_ID, state=m, updated_at=utc_now())
    session.add(row)
    await session.flush()
    return row


async def _locked_market(session) -> SiteFunMarket:
    row = (
        await session.execute(select(SiteFunMarket).where(SiteFunMarket.id == MARKET_ID).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _INIT_LOCK_KEY})
        row = (
            await session.execute(select(SiteFunMarket).where(SiteFunMarket.id == MARKET_ID).with_for_update())
        ).scalar_one_or_none()
        if row is None:
            row = await _create_market(session)
    return row


async def _advance(session, row: SiteFunMarket) -> dict:
    m = row.state
    t = now_ms()
    if t > m["t"]:
        c = _Collector()
        sm.advance_to(m, t, c)
        await _flush_collector(session, c)
        row.state = m
        flag_modified(row, "state")  # 原地改的 JSON，要显式标脏才会写回
        row.updated_at = utc_now()
        await session.flush()
    return row.state


async def market_for_update(session) -> dict:
    """加锁并推进到现在（下单用）。"""
    return await _advance(session, await _locked_market(session))


async def market_for_read(session) -> dict:
    """读接口：新鲜就直接用（不加锁不写），否则加锁推进。"""
    row = (
        await session.execute(select(SiteFunMarket.state).where(SiteFunMarket.id == MARKET_ID))
    ).scalar_one_or_none()
    if row is not None and now_ms() - row["t"] < FRESH_MS:
        return row
    state = await market_for_update(session)
    await session.commit()
    return state


async def ensure_market(session) -> None:
    """种子脚本用：开张（已开张就推进到现在）。"""
    await market_for_update(session)


# ---------------- 账户 ----------------


async def _lock_account(session, user_id: str) -> SiteFunStockAccount:
    await session.execute(
        insert(SiteFunStockAccount)
        .values(user_id=user_id, realized=0, dividends=0, dividend_carry=0)
        .on_conflict_do_nothing(index_elements=[SiteFunStockAccount.user_id])
    )
    return (
        await session.execute(
            select(SiteFunStockAccount).where(SiteFunStockAccount.user_id == user_id).with_for_update()
        )
    ).scalar_one()


def _stock(m: dict, symbol: str) -> dict:
    s = next((x for x in m["stocks"] if x["symbol"] == (symbol or "").upper()), None)
    if s is None:
        raise not_found()
    return s


def _price_map(m: dict) -> dict[str, float]:
    return {s["symbol"]: s["series"]["last"] for s in m["stocks"]}


def trade_dto(t: SiteFunStockTrade) -> dict:
    return {
        "id": str(t.id), "clientOrderId": t.client_order_id, "symbol": t.symbol, "side": t.side,
        "qty": int(t.qty), "avgPrice": t.avg_price, "gross": int(t.gross), "fee": int(t.fee),
        "total": int(t.total), "realized": int(t.realized), "createdAt": iso_utc(t.created_at),
    }


def positions_dto(positions, prices: dict[str, float]) -> list[dict]:
    out = []
    for p in positions:
        if p.qty <= 0:
            continue
        price = prices.get(p.symbol, 0.0)
        market_value = sm.js_round(price * p.qty)
        cost = p.avg_cost * p.qty
        out.append(
            {
                "symbol": p.symbol, "qty": int(p.qty), "avgCost": sm.round2(p.avg_cost),
                "marketValue": market_value, "unrealized": sm.js_round(market_value - cost),
                "unrealizedPct": (market_value - cost) / cost if cost else 0,
            }
        )
    out.sort(key=lambda x: -x["marketValue"])
    return out


async def account_dto(session, m: dict, user_id: str) -> dict:
    w = await lock_wallet(session, user_id)
    acc = await session.get(SiteFunStockAccount, user_id)
    positions = (
        await session.execute(select(SiteFunStockPosition).where(SiteFunStockPosition.user_id == user_id))
    ).scalars().all()
    pos = positions_dto(positions, _price_map(m))
    trades = (
        await session.execute(
            select(SiteFunStockTrade)
            .where(SiteFunStockTrade.user_id == user_id)
            .order_by(SiteFunStockTrade.id.desc())
            .limit(50)
        )
    ).scalars().all()
    return {
        "balance": int(w.balance),
        "positions": pos,
        "stockValue": sum(p["marketValue"] for p in pos),
        "realized": sm.js_round(acc.realized) if acc else 0,
        "dividends": int(acc.dividends) if acc else 0,
        "trades": [trade_dto(t) for t in trades],
    }


# ---------------- 序列化 ----------------


def phase_dto(t: int) -> dict:
    idx = sm.phase_index(t)
    start = sm.phase_start(idx)
    i = idx % 6
    return {"index": i, "name": STOCK_PHASE_NAMES[i], "startsAt": iso_ms(start), "endsAt": iso_ms(start + sm.GAME_DAY_MS)}


def spark_of(points: list, last: float) -> list[float]:
    out: list[float] = []
    slot = -1
    for t, price in points:
        k = t // SPARK_STEP_MS
        if k == slot:
            out[-1] = price
        else:
            out.append(price)
            slot = k
    if out:
        out[-1] = last
    return out[-48:]


def quote_dto(s: dict) -> dict:
    x = s["series"]
    lo, hi = sm.PARAMS["dailyImpactRange"]
    return {
        "symbol": s["symbol"],
        "price": x["last"],
        "prevClose": x["prevClose"],
        "open": x["today"]["open"],
        "high": x["today"]["high"],
        "low": x["today"]["low"],
        "change": sm.round2(x["last"] - x["prevClose"]),
        "changePct": (x["last"] - x["prevClose"]) / x["prevClose"] if x["prevClose"] else 0,
        "volume": int(x["today"]["volume"]),
        "limitUp": s["dayImpact"] >= hi - 1e-9,
        "limitDown": s["dayImpact"] <= lo + 1e-9,
        "spark": spark_of(x["intraday"], x["last"]),
    }


def bar_dto(b: dict, kind: str) -> dict:
    date = sm.beijing_date(int(b["start"]))[5:]
    label = date if kind == "daily" else f"{date} {STOCK_PHASE_NAMES[sm.phase_index(int(b['start'])) % 6]}"
    return {
        "startsAt": iso_ms(int(b["start"])), "label": label, "open": b["open"], "high": b["high"],
        "low": b["low"], "close": b["close"], "volume": int(b["volume"]),
    }


def news_dto(n: SiteFunStockNews) -> dict:
    return {
        "id": str(n.id), "publishedAt": iso_utc(n.published_at), "kind": n.kind, "scope": n.scope,
        "symbols": list(n.symbols or []), "sector": n.sector, "direction": n.direction,
        "strength": int(n.strength), "headline": n.headline, "summary": n.summary,
        "sourceName": n.source_name, "sourceUrl": n.source_url,
    }


# ---------------- 接口 ----------------


def companies() -> list[dict]:
    return [dict(c) for c in STOCK_COMPANIES]


async def market_snapshot(session) -> dict:
    m = await market_for_read(session)
    ix = m["index"]
    p = sm.PARAMS
    return {
        "asOf": iso_ms(m["t"]),
        "seq": m["seq"],
        "tradingDate": sm.beijing_date(m["t"]),
        "phase": phase_dto(m["t"]),
        "index": {
            "value": ix["last"], "prevClose": ix["prevClose"],
            "changePct": (ix["last"] - ix["prevClose"]) / ix["prevClose"] if ix["prevClose"] else 0,
        },
        "quotes": [quote_dto(s) for s in m["stocks"]],
        "rules": {
            "feeBp": p["feeBp"], "stampBp": p["stampBp"], "maxOrderValue": p["maxOrderValue"],
            "maxPositionValue": p["maxPositionValue"], "defaultMaxSlipBp": p["defaultMaxSlipBp"],
            "nextDividendAt": iso_ms(m["nextDividendAt"]),
        },
    }


async def history(session, symbol: str, range_: str | None) -> dict:
    range_ = range_ or "intraday"
    if range_ not in ("intraday", "phase", "daily"):
        raise invalid({"range": "只能是 intraday、phase 或 daily"})
    m = await market_for_read(session)
    symbol = (symbol or "").upper()
    x = m["index"] if symbol == "INDEX" else _stock(m, symbol)["series"]
    if range_ == "intraday":
        return {"symbol": symbol, "range": range_, "intraday": [{"t": iso_ms(t), "price": p} for t, p in x["intraday"]], "bars": []}
    limit = sm.MAX_DAILY_BARS if range_ == "daily" else sm.MAX_PHASE_BARS
    rows = (
        await session.execute(
            select(SiteFunStockBar)
            .where(SiteFunStockBar.symbol == symbol, SiteFunStockBar.kind == range_)
            .order_by(SiteFunStockBar.start_ms.desc())
            .limit(limit)
        )
    ).scalars().all()
    bars = [
        {"start": r.start_ms, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
        for r in reversed(rows)
    ]
    # 历史 K 线 + 正在走的这一根
    current = x["today"] if range_ == "daily" else x["phase"]
    bars = [b for b in bars if b["start"] < current["start"]] + [current]
    return {"symbol": symbol, "range": range_, "intraday": [], "bars": [bar_dto(b, range_) for b in bars]}


async def news(session, symbol: str | None, limit_raw) -> list[dict]:
    await market_for_read(session)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 30
    limit = min(100, max(1, limit or 30))
    rows = (
        await session.execute(
            select(SiteFunStockNews).order_by(SiteFunStockNews.published_at.desc(), SiteFunStockNews.id.desc()).limit(MAX_NEWS)
        )
    ).scalars().all()
    if symbol:
        symbol = symbol.upper()
        sector = COMPANY_BY_SYMBOL.get(symbol, {}).get("sector")
        rows = [
            n for n in rows
            if n.scope == "market" or symbol in (n.symbols or []) or (n.scope == "sector" and n.sector == sector)
        ]
    return [news_dto(n) for n in rows[:limit]]


async def me(session, user_id: str) -> dict:
    m = await market_for_read(session)
    return await account_dto(session, m, user_id)


async def leaderboard(session, user_id: str) -> list[dict]:
    m = await market_for_read(session)
    await lock_wallet(session, user_id)
    prices = _price_map(m)
    wallets = (
        await session.execute(
            select(SiteFunWallet.user_id, SiteFunWallet.balance)
            .join(User, User.id == SiteFunWallet.user_id)
            .where(User.merged_into_id.is_(None))
        )
    ).all()
    stock_value: dict[str, int] = {}
    for p in (await session.execute(select(SiteFunStockPosition).where(SiteFunStockPosition.qty > 0))).scalars():
        stock_value[p.user_id] = stock_value.get(p.user_id, 0) + sm.js_round(prices.get(p.symbol, 0) * p.qty)
    ranked = sorted(
        (
            {"id": uid, "stockValue": stock_value.get(uid, 0), "totalAssets": int(bal) + stock_value.get(uid, 0)}
            for uid, bal in wallets
        ),
        key=lambda e: (-e["totalAssets"], e["id"]),
    )[:20]
    users = await load_users(session, [e["id"] for e in ranked])
    ranked = [e for e in ranked if e["id"] in users]
    return [
        {"rank": i + 1, "user": users[e["id"]], "totalAssets": e["totalAssets"], "stockValue": e["stockValue"]}
        for i, e in enumerate(ranked)
    ]


def parse_draft(body: dict) -> dict:
    symbol, side, qty = body.get("symbol"), body.get("side"), body.get("qty")
    if not isinstance(symbol, str) or not symbol:
        raise invalid({"symbol": "缺少股票代码"})
    if side not in ("buy", "sell"):
        raise invalid({"side": "只能是 buy 或 sell"})
    if isinstance(qty, float) and qty.is_integer():
        qty = int(qty)
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise invalid({"qty": "股数要是正整数"})
    return {"symbol": symbol.upper(), "side": side, "qty": qty}


def _fmt(n: int) -> str:
    return f"{n:,}"


async def evaluate(session, m: dict, user_id: str, d: dict, *, lock: bool) -> tuple[dict, dict]:
    s = _stock(m, d["symbol"])
    q = sm.preview_fill(s, d["side"], d["qty"])
    if lock:
        w = await lock_wallet(session, user_id)
        pos = await session.get(SiteFunStockPosition, (user_id, s["symbol"]), with_for_update=True)
    else:
        w = await lock_wallet(session, user_id)
        pos = await session.get(SiteFunStockPosition, (user_id, s["symbol"]))
    held = int(pos.qty) if pos else 0
    p = sm.PARAMS
    blocked = None
    if q["gross"] > p["maxOrderValue"]:
        blocked = {"reason": "order_limit", "message": f"单笔最多成交 {_fmt(p['maxOrderValue'])} 类脑币"}
    elif d["side"] == "sell" and d["qty"] > held:
        blocked = {"reason": "shares", "message": f"只持有 {held} 股"}
    elif d["side"] == "buy" and q["total"] > int(w.balance):
        blocked = {"reason": "funds", "message": "类脑币不够"}
    elif d["side"] == "buy" and (pos.avg_cost * held if pos else 0) + q["gross"] > p["maxPositionValue"]:
        blocked = {"reason": "position_limit", "message": f"每只股票最多持有 {_fmt(p['maxPositionValue'])} 类脑币（按成本）"}
    elif q["dailyLimited"]:
        blocked = {
            "reason": "daily_limit",
            "message": "这只股票本阶段被买得太多，下个阶段再来" if d["side"] == "buy" else "这只股票本阶段被卖得太多，下个阶段再来",
        }
    preview = {
        **d, "symbol": s["symbol"], "price": q["price"], "avgPrice": q["avgPrice"], "gross": q["gross"],
        "fee": q["fee"], "total": q["total"], "priceAfter": q["priceAfter"], "blocked": blocked,
    }
    return preview, {"stock": s, "fill": q, "wallet": w, "position": pos}


async def preview(session, user_id: str, body: dict) -> dict:
    d = parse_draft(body)
    m = await market_for_read(session)
    p, _ = await evaluate(session, m, user_id, d, lock=False)
    return p


async def order(session, user_id: str, body: dict) -> dict:
    client_order_id = body.get("clientOrderId")
    if not isinstance(client_order_id, str) or not client_order_id or len(client_order_id) > 100:
        raise invalid({"clientOrderId": "缺少订单号"})
    m = await market_for_update(session)
    # 幂等：同一个订单号只成交一次，返回第一次的结果
    done = (
        await session.execute(
            select(SiteFunStockTrade).where(
                SiteFunStockTrade.user_id == user_id, SiteFunStockTrade.client_order_id == client_order_id
            )
        )
    ).scalar_one_or_none()
    if done is not None:
        return {"trade": trade_dto(done), "account": await account_dto(session, m, user_id)}
    d = parse_draft(body)
    ref = body.get("refPrice")
    if isinstance(ref, bool) or not isinstance(ref, (int, float)) or ref <= 0:
        raise invalid({"refPrice": "缺少下单时看到的价格"})
    slip = body.get("maxSlipBp")
    max_slip = slip if isinstance(slip, (int, float)) and not isinstance(slip, bool) and slip >= 0 else sm.PARAMS["defaultMaxSlipBp"]

    p, ctx = await evaluate(session, m, user_id, d, lock=True)
    if p["blocked"]:
        raise SiteError(422, "validation_failed", p["blocked"]["message"], {"reason": p["blocked"]["reason"]})
    # 拿成交前的报价比（不含这笔单自己的冲击）
    if abs(p["price"] / ref - 1) * 10_000 > max_slip:
        raise SiteError(
            409, "conflict", "价格变了，请确认新价格再下单",
            {"reason": "slippage", "price": p["price"], "avgPrice": p["avgPrice"]},
        )
    s, fill, w, pos = ctx["stock"], ctx["fill"], ctx["wallet"], ctx["position"]
    acc = await _lock_account(session, user_id)
    if pos is None:
        pos = SiteFunStockPosition(user_id=user_id, symbol=s["symbol"], qty=0, avg_cost=0)
        session.add(pos)
    realized = 0.0
    if d["side"] == "buy":
        credit(w, -p["total"])
        pos.avg_cost = (pos.avg_cost * pos.qty + p["gross"]) / (pos.qty + d["qty"])
        pos.qty = int(pos.qty) + d["qty"]
    else:
        credit(w, p["total"])
        realized = p["total"] - pos.avg_cost * d["qty"]
        pos.qty = int(pos.qty) - d["qty"]
        if pos.qty == 0:
            await session.delete(pos)
    acc.realized = float(acc.realized) + realized
    sm.apply_fill(s, fill, d["qty"])
    row = await session.get(SiteFunMarket, MARKET_ID)
    row.state = m
    flag_modified(row, "state")
    row.updated_at = utc_now()
    trade = SiteFunStockTrade(
        user_id=user_id, client_order_id=client_order_id, symbol=s["symbol"], side=d["side"], qty=d["qty"],
        avg_price=p["avgPrice"], gross=p["gross"], fee=p["fee"], total=p["total"], realized=sm.js_round(realized),
    )
    session.add(trade)
    await session.flush()
    # 流水只留最近 200 笔（幂等也只保证最近 200 个订单号，和假后端一样）
    keep = (
        select(SiteFunStockTrade.id).where(SiteFunStockTrade.user_id == user_id)
        .order_by(SiteFunStockTrade.id.desc()).limit(200)
    )
    await session.execute(
        delete(SiteFunStockTrade).where(
            SiteFunStockTrade.user_id == user_id, SiteFunStockTrade.id.not_in(keep.scalar_subquery())
        )
    )
    return {"trade": trade_dto(trade), "account": await account_dto(session, m, user_id)}
