"""娱乐区（前端草案 /fun，协议里还没有；以 web 仓库 src/mock/handlers/fun.ts、stocks.ts 为准）。

钱包：GET /fun/wallet、POST /fun/wallet/daily、GET /fun/leaderboard
21 点：GET /fun/blackjack/rounds/current、POST /fun/blackjack/rounds、POST /fun/blackjack/rounds/{id}/actions
股市：GET /fun/stocks/companies、/market、/{symbol}/history、/news、/me、/leaderboard，
     POST /fun/stocks/orders/preview、/fun/stocks/orders
股市的公司列表和行情、新闻不要求登录（假后端也不要求）；钱包、牌局、持仓、下单、排行榜要登录。
"""

from fastapi import APIRouter, Body, Query

from site_v1.deps import CurrentUser, get_session_factory
from site_v1.services.fun import blackjack, stocks, wallet

router = APIRouter(tags=["娱乐区"])


async def run(fn, *args):
    async with get_session_factory()() as session:
        result = await fn(session, *args)
        await session.commit()
        return result


def _body(body) -> dict:
    return body if isinstance(body, dict) else {}


# ---------------- 钱包 ----------------


@router.get("/fun/wallet", summary="娱乐账户")
async def get_wallet(viewer: CurrentUser):
    return await run(wallet.get_wallet, viewer.user_id)


@router.post("/fun/wallet/daily", summary="每日领取")
async def claim_daily(viewer: CurrentUser):
    return await run(wallet.claim_daily, viewer.user_id)


@router.get("/fun/leaderboard", summary="类脑币排行榜")
async def fun_leaderboard(viewer: CurrentUser):
    return await run(wallet.leaderboard, viewer.user_id)


# ---------------- 21 点 ----------------


@router.get("/fun/blackjack/rounds/current", summary="进行中的一局")
async def current_round(viewer: CurrentUser):
    return await run(blackjack.current_round, viewer.user_id)


@router.post("/fun/blackjack/rounds", summary="开局下注")
async def start_round(viewer: CurrentUser, body: dict | None = Body(None)):
    return await run(blackjack.start, viewer.user_id, _body(body))


@router.post("/fun/blackjack/rounds/{id}/actions", summary="要牌、停牌、加倍")
async def round_action(id: str, viewer: CurrentUser, body: dict | None = Body(None)):
    return await run(blackjack.act, viewer.user_id, id, _body(body))


# ---------------- 股市 ----------------


@router.get("/fun/stocks/companies", summary="上市公司")
async def stock_companies():
    return stocks.companies()


@router.get("/fun/stocks/market", summary="全市场快照")
async def stock_market():
    return await run(stocks.market_snapshot)


@router.get("/fun/stocks/news", summary="新闻和传闻")
async def stock_news(symbol: str | None = Query(None), limit: str | None = Query(None)):
    return await run(stocks.news, symbol, limit)


@router.get("/fun/stocks/me", summary="我的持仓和最近成交")
async def stock_me(viewer: CurrentUser):
    return await run(stocks.me, viewer.user_id)


@router.get("/fun/stocks/leaderboard", summary="总资产排行榜")
async def stock_leaderboard(viewer: CurrentUser):
    return await run(stocks.leaderboard, viewer.user_id)


@router.get("/fun/stocks/{symbol}/history", summary="分时、阶段 K、日 K")
async def stock_history(symbol: str, range: str | None = Query(None)):
    return await run(stocks.history, symbol, range)


@router.post("/fun/stocks/orders/preview", summary="下单预估")
async def stock_preview(viewer: CurrentUser, body: dict | None = Body(None)):
    return await run(stocks.preview, viewer.user_id, _body(body))


@router.post("/fun/stocks/orders", summary="下单（立即按服务端价格成交）")
async def stock_order(viewer: CurrentUser, body: dict | None = Body(None)):
    return await run(stocks.order, viewer.user_id, _body(body))
