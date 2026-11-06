"""模拟股市的价格模型：照搬前端假后端 src/mock/stocks/model.ts 的公式和顺序（纯数据 + 纯函数）。

每股价格 = 0.8 × V × H × R × T × (1 + 0.5 × (S − 1)) / N × exp(临时冲击 + 个股周期)，
显示和成交再乘一点盘口噪声。状态全是 dict / list / 数字，可以直接存 JSON。

和假后端的差别：
- 收好的日 K、阶段 K 不留在状态里，通过 on_bar 回调交给调用方写库（状态里 daily/phases 为空）；
- 分时点存成 [t, price] 二元组；
- 补跑步数上限 5000 步（假后端 20000），Python 慢一些。
时间：现实一天 6 个阶段（每段 4 小时，北京时间 0/4/8/12/16/20 点），模型内部一个阶段 = 一个「游戏日」。
随机数只在推进时间里用，下单不消耗随机数（同状态预览和成交结果一样）。
"""

import math

from site_v1.services.fun.stock_companies import STOCK_COMPANIES, STOCK_SECTORS

# ---------- 时间 ----------

GAME_DAY_MS = 4 * 3600_000
DAY_MS = 86400_000
BEIJING_OFFSET_MS = 8 * 3600_000
TICK_MS = 2_000
INTRADAY_STEP_MS = 5 * 60_000
MAX_DAILY_BARS = 365
MAX_PHASE_BARS = 180
DIVIDEND_HOUR_BEIJING = 20
MAX_CATCH_UP_MS = 7 * DAY_MS
MAX_STEPS = 5000


def phase_index(t: int) -> int:
    return (t + BEIJING_OFFSET_MS) // GAME_DAY_MS


def phase_start(idx: int) -> int:
    return idx * GAME_DAY_MS - BEIJING_OFFSET_MS


def date_index(t: int) -> int:
    return (t + BEIJING_OFFSET_MS) // DAY_MS


def date_start(idx: int) -> int:
    return idx * DAY_MS - BEIJING_OFFSET_MS


def beijing_date(t: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp((t + BEIJING_OFFSET_MS) / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")


def next_dividend_at(t: int) -> int:
    today = date_start(date_index(t)) + DIVIDEND_HOUR_BEIJING * 3600_000
    return today if today > t else today + DAY_MS


# ---------- 参数（每游戏日） ----------

PARAMS = {
    "growth": 0.005,
    "profitTheta": 0.03,
    "profitSigma": 0.015,
    "anchorK": 0.08,
    "profitBase": (-0.08, 0.12),
    "profitRedrawDays": 120,
    "hypeTheta": 0.05,
    "hypeSigma": 0.015,
    "hypeMin": 0.4,
    "hypeMax": 2.5,
    "sectorTheta": 0.05,
    "sectorSigma": 0.02,
    "sectorWeight": 0.5,
    "trendTheta": 0.01,
    "trendSigma": 0.006,
    "trendMin": 0.5,
    "trendMax": 2,
    "cycleStep": (0.0015, 0.005),
    "cycleStepVolatile": (0.004, 0.01),
    "cycleTop": (1.1, 1.25),
    "cycleBottom": (0.8, 0.92),
    "stockCycleStep": (0.003, 0.008),
    "stockCycleAmp": (0.2, 0.7),
    "stockCycleEarlyFlip": 0.01,
    "microSigma": 0.001,
    "alpha": 0.4,
    "liquidity": 100_000,
    "impactPermanent": 0.3,
    "impactHalfLifeMs": 60_000,
    "dailyImpactRange": (0.9, 1.1),
    "feeBp": 10,
    "stampBp": 10,
    "maxOrderValue": 2_000,
    "maxPositionValue": 5_000,
    "defaultMaxSlipBp": 100,
    "dividendBase": 0.0002,
    "dividendSlope": 0.0008,
}

# ---------- 随机数（mulberry32，状态是一个 uint32） ----------

M32 = 0xFFFFFFFF


def next_random(state: dict) -> float:
    state["rng"] = (state["rng"] + 0x6D2B79F5) & M32
    t = state["rng"]
    t = ((t ^ (t >> 15)) * (t | 1)) & M32
    t = (t ^ ((t + (((t ^ (t >> 7)) * (t | 61)) & M32)) & M32)) & M32
    return ((t ^ (t >> 14)) & M32) / 4294967296


def uniform(state: dict, rng: tuple) -> float:
    a, b = rng
    return a + (b - a) * next_random(state)


def gauss(state: dict) -> float:
    u = max(next_random(state), 1e-12)
    return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * next_random(state))


def clamp(x: float, a: float, b: float) -> float:
    return min(b, max(a, x))


def round2(x: float) -> float:
    # 和 JS 的 Math.round(x * 100) / 100 一致（.5 向上）
    return math.floor(x * 100 + 0.5) / 100


def js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


# ---------- 状态 ----------


def model_price(m: dict, s: dict) -> float:
    p = PARAMS
    return (
        (0.8 * s["V"] * s["H"] * m["R"] * m["T"] * (1 + p["sectorWeight"] * (m["S"][s["sector"]] - 1)))
        / s["N"]
        * math.exp(s["temp"] + s["cyc"])
    )


def index_value(m: dict) -> float:
    return sum(s["series"]["last"] / s["ipoPrice"] for s in m["stocks"]) / len(m["stocks"]) * 1000


def new_bar(start: int, price: float) -> dict:
    return {"start": start, "open": price, "high": price, "low": price, "close": price, "volume": 0}


def new_series(symbol: str, t: int, price: float) -> dict:
    return {
        "symbol": symbol,
        "last": price,
        "prevClose": price,
        "today": new_bar(date_start(date_index(t)), price),
        "phase": new_bar(phase_start(phase_index(t)), price),
        "intraday": [[t // INTRADAY_STEP_MS * INTRADAY_STEP_MS, price]],
    }


def touch(x: dict, price: float, volume: int = 0) -> None:
    x["last"] = price
    for b in (x["today"], x["phase"]):
        b["high"] = max(b["high"], price)
        b["low"] = min(b["low"], price)
        b["close"] = price
        b["volume"] += volume


def create_market(t: int, seed: int) -> dict:
    m = {
        "rng": seed & M32,
        "t": t,
        "seq": 0,
        "phase": phase_index(t),
        "date": date_index(t),
        "R": 1,
        "dir": 1,
        "top": 1.1,
        "bottom": 0.9,
        "volatileUntil": 0,
        "T": 1,
        "trendRamps": [],
        "S": {x["id"]: 1 for x in STOCK_SECTORS},
        "stocks": [],
        "index": new_series("INDEX", t, 1000),
        "nextDividendAt": next_dividend_at(t),
    }
    m["top"] = uniform(m, PARAMS["cycleTop"])
    m["bottom"] = uniform(m, PARAMS["cycleBottom"])
    for c in STOCK_COMPANIES:
        H = uniform(m, (0.95, 1.05))
        N = 1_000_000
        V = (c["ipoPrice"] * N) / (0.8 * H)
        P0 = uniform(m, PARAMS["profitBase"])
        m["stocks"].append(
            {
                "symbol": c["symbol"],
                "sector": c["sector"],
                "ipoPrice": c["ipoPrice"],
                "N": N,
                "V": V,
                "anchor": V,
                "P": P0,
                "P0": P0,
                "H": H,
                "temp": 0,
                "dayImpact": 1,
                "cyc": 0,
                "cycDir": 1,
                "cycTurn": 0,
                "ramps": [],
                "series": new_series(c["symbol"], t, c["ipoPrice"]),
            }
        )
    return m


# ---------- 推进 ----------


def apply_ramps(ramps: list, ms: int, apply) -> list:
    for r in ramps:
        ln = r["lnLeft"] if r["msLeft"] <= ms else r["lnLeft"] * ms / r["msLeft"]
        apply(ln)
        r["lnLeft"] -= ln
        r["msLeft"] -= ms
    return [r for r in ramps if r["msLeft"] > 0 and abs(r["lnLeft"]) > 1e-9]


def step(m: dict, ms: int) -> None:
    d = ms / GAME_DAY_MS
    sq = math.sqrt(d)
    p = PARAMS

    volatile = m["t"] < m["volatileUntil"]
    m["R"] += m["dir"] * uniform(m, p["cycleStepVolatile"] if volatile else p["cycleStep"]) * d
    if m["dir"] > 0 and m["R"] > m["top"]:
        m["dir"] = -1
        m["top"] = uniform(m, p["cycleTop"])
    elif m["dir"] < 0 and m["R"] < m["bottom"]:
        m["dir"] = 1
        m["bottom"] = uniform(m, p["cycleBottom"])

    m["T"] += p["trendTheta"] * (1 - m["T"]) * d + p["trendSigma"] * sq * gauss(m)

    def trend(ln):
        m["T"] *= math.exp(ln)

    m["trendRamps"] = apply_ramps(m["trendRamps"], ms, trend)
    m["T"] = clamp(m["T"], p["trendMin"], p["trendMax"])

    for sid in list(m["S"]):
        m["S"][sid] = clamp(
            m["S"][sid] + p["sectorTheta"] * (1 - m["S"][sid]) * d + p["sectorSigma"] * sq * gauss(m),
            0.6,
            1.6,
        )

    for s in m["stocks"]:
        s["cyc"] += s["cycDir"] * uniform(m, p["stockCycleStep"]) * d
        passed = s["cyc"] >= s["cycTurn"] if s["cycDir"] > 0 else s["cyc"] <= s["cycTurn"]
        if passed or next_random(m) < p["stockCycleEarlyFlip"] * d:
            s["cycDir"] = -1 if s["cycDir"] > 0 else 1
            s["cycTurn"] = s["cycDir"] * uniform(m, p["stockCycleAmp"])
        if next_random(m) < d / p["profitRedrawDays"]:
            s["P0"] = uniform(m, p["profitBase"])
        target = s["P0"] - p["anchorK"] * math.log(s["V"] / s["anchor"])
        s["P"] += p["profitTheta"] * (target - s["P"]) * d + p["profitSigma"] * sq * gauss(m)
        if s["H"] < 0.5:
            s["P"] *= 1 - (1 - s["H"]) * 0.005 * d
        s["V"] *= math.exp(p["growth"] * s["P"] * d)
        s["H"] += p["hypeTheta"] * (1 - s["H"]) * d + p["hypeSigma"] * sq * gauss(m)

        def hype(ln, s=s):
            s["H"] *= math.exp(ln)

        s["ramps"] = apply_ramps(s["ramps"], ms, hype)
        s["H"] = clamp(s["H"], p["hypeMin"], p["hypeMax"])
        s["temp"] *= 0.5 ** (ms / p["impactHalfLifeMs"])
    m["t"] += ms


def quote(m: dict, s: dict, noise: float = 0.0) -> float:
    return max(0.01, round2(model_price(m, s) * math.exp(PARAMS["microSigma"] * noise)))


def all_series(m: dict) -> list:
    return [s["series"] for s in m["stocks"]] + [m["index"]]


def refresh_quotes(m: dict) -> None:
    for s in m["stocks"]:
        touch(s["series"], quote(m, s, gauss(m)))
    touch(m["index"], round2(index_value(m)))
    m["seq"] += 1


class Hooks:
    """推进时的回调：每步之前（按时间出新闻）、分红、收好一根 K 线。"""

    def before_step(self, m: dict, t: int, ms: int) -> None:  # pragma: no cover - 默认空
        pass

    def on_dividend(self, m: dict, at: int, per_share: dict) -> None:  # pragma: no cover
        pass

    def on_bar(self, symbol: str, kind: str, bar: dict) -> None:  # pragma: no cover
        pass


def roll_phase(m: dict, idx: int, hooks: Hooks) -> None:
    for x in all_series(m):
        hooks.on_bar(x["symbol"], "phase", dict(x["phase"]))
        x["phase"] = new_bar(phase_start(idx), x["last"])
    for s in m["stocks"]:
        s["dayImpact"] = 1
    m["phase"] = idx


def roll_date(m: dict, idx: int, hooks: Hooks) -> None:
    for x in all_series(m):
        hooks.on_bar(x["symbol"], "daily", dict(x["today"]))
        x["prevClose"] = x["last"]
        x["today"] = new_bar(date_start(idx), x["last"])
        x["intraday"] = [[date_start(idx), x["last"]]]
    m["date"] = idx


def record_intraday(m: dict) -> None:
    slot = m["t"] // INTRADAY_STEP_MS * INTRADAY_STEP_MS
    for x in all_series(m):
        pts = x["intraday"]
        if not pts or slot > pts[-1][0]:
            pts.append([slot, x["last"]])
        else:
            pts[-1][1] = x["last"]


def roll_if_needed(m: dict, hooks: Hooks) -> None:
    ph = phase_index(m["t"])
    dt = date_index(m["t"])
    if ph == m["phase"] and dt == m["date"]:
        return
    refresh_quotes(m)
    if ph != m["phase"]:
        roll_phase(m, ph, hooks)
    if dt != m["date"]:
        roll_date(m, dt, hooks)


def pay_dividend(m: dict, hooks: Hooks) -> None:
    per_share = {}
    for s in m["stocks"]:
        price = model_price(m, s)
        dps = price * (PARAMS["dividendBase"] + PARAMS["dividendSlope"] * clamp(s["P"], 0, 0.2))
        per_share[s["symbol"]] = dps
        # 除息：价格下调同样金额，分红不凭空送钱
        s["V"] *= 1 - dps / price
    hooks.on_dividend(m, m["t"], per_share)


def advance_to(m: dict, until: int, hooks: Hooks | None = None, min_step_ms: int = TICK_MS) -> None:
    """把行情推进到 until（毫秒）。离开很久回来时放大步长，最多补 7 天。"""
    hooks = hooks or Hooks()
    if until - m["t"] > MAX_CATCH_UP_MS:
        m["t"] = until - MAX_CATCH_UP_MS
        m["nextDividendAt"] = next_dividend_at(m["t"])
        roll_if_needed(m, hooks)
    step_ms = max(min_step_ms, math.ceil((until - m["t"]) / MAX_STEPS))
    while m["t"] < until:
        ms = min(step_ms, until - m["t"])
        hooks.before_step(m, m["t"], ms)
        slot_before = m["t"] // INTRADAY_STEP_MS
        step(m, ms)
        roll_if_needed(m, hooks)
        if m["t"] >= m["nextDividendAt"]:
            pay_dividend(m, hooks)
            m["nextDividendAt"] = next_dividend_at(m["t"])
        if m["t"] // INTRADAY_STEP_MS != slot_before:
            refresh_quotes(m)
            record_intraday(m)
    refresh_quotes(m)
    record_intraday(m)


# ---------- 新闻冲击 ----------


def push_hype(s: dict, mul: float, ramp_ms: int) -> None:
    s["ramps"].append({"lnLeft": math.log(mul), "msLeft": ramp_ms})


def push_trend(m: dict, mul: float, ramp_ms: int) -> None:
    m["trendRamps"].append({"lnLeft": math.log(mul), "msLeft": ramp_ms})


def shift_profit(s: dict, delta: float) -> None:
    s["P"] += delta


def mark_volatile(m: dict, game_days: float) -> None:
    m["volatileUntil"] = max(m["volatileUntil"], m["t"] + game_days * GAME_DAY_MS)


# ---------- 成交 ----------


def preview_fill(s: dict, side: str, qty: int) -> dict:
    """预估一笔成交（预览和真正成交共用），按当前报价 series.last。"""
    p = PARAMS
    price = s["series"]["last"]
    signed = qty if side == "buy" else -qty
    k = max(0.5, 1 + (p["alpha"] * signed * price) / p["liquidity"])
    lo, hi = p["dailyImpactRange"]
    after = s["dayImpact"] * k
    daily_limited = after > hi if side == "buy" else after < lo
    avg_price = round2(price * (1 + k) / 2)
    gross = js_round(avg_price * qty)
    fee = js_round(gross * (p["feeBp"] if side == "buy" else p["feeBp"] + p["stampBp"]) / 10_000)
    total = gross + fee if side == "buy" else gross - fee
    return {
        "price": price,
        "k": k,
        "avgPrice": avg_price,
        "gross": gross,
        "fee": fee,
        "total": total,
        "priceAfter": round2(price * k),
        "dailyLimited": daily_limited,
    }


def apply_fill(s: dict, q: dict, qty: int) -> None:
    """成交后的价格冲击：30% 永久进情绪，70% 临时（按半衰期回落）。"""
    ln = math.log(q["k"])
    s["H"] = clamp(s["H"] * math.exp(PARAMS["impactPermanent"] * ln), PARAMS["hypeMin"], PARAMS["hypeMax"])
    s["temp"] += (1 - PARAMS["impactPermanent"]) * ln
    s["dayImpact"] *= q["k"]
    touch(s["series"], q["priceAfter"], qty)
