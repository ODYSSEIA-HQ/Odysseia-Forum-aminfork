"""娱乐区（前端草案 /fun，协议里还没有）：钱包、21 点、模拟股市。

类脑币是站内虚拟积分，整数；所有改余额的操作都在一个事务里锁钱包行。
牌靴和行情状态只在服务端（不下发）。
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, Float, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from shared.time_utils import utc_now


class SiteFunWallet(SQLModel, table=True):
    """娱乐账户（21 点和股市共用）。"""

    __tablename__ = "site_fun_wallet"

    user_id: str = Field(primary_key=True)
    balance: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    last_daily_at: datetime | None = None
    """上次每日领取（按北京时间自然日判断）。"""

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SiteFunBlackjackRound(SQLModel, table=True):
    """一局 21 点。player/dealer 是完整手牌（庄家暗牌结算前不下发）。"""

    __tablename__ = "site_fun_bj_round"
    __table_args__ = (Index("ix_site_fun_bj_round_user_state", "user_id", "state"),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    user_id: str
    bet: int = Field(sa_column=Column(BigInteger, nullable=False))
    doubled: bool = False
    player: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    dealer: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    state: str = "playing"
    """playing / settled。"""

    outcome: str | None = None
    payout: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    created_at: datetime = Field(default_factory=utc_now)
    settled_at: datetime | None = None


class SiteFunBlackjackShoe(SQLModel, table=True):
    """每个账号自己的牌靴（剩下的牌，从前往后发）。"""

    __tablename__ = "site_fun_bj_shoe"

    user_id: str = Field(primary_key=True)
    cards: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    size: int = Field(default=0, sa_column=Column(Integer, nullable=False))


class SiteFunMarket(SQLModel, table=True):
    """全站唯一的股市状态（id=1）：价格模型的全部状态 + 今天的分时，不含历史 K 线。"""

    __tablename__ = "site_fun_market"

    id: int = Field(sa_column=Column(Integer, primary_key=True))
    state: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    updated_at: datetime = Field(default_factory=utc_now)


class SiteFunStockBar(SQLModel, table=True):
    """收好的 K 线：kind=daily（日 K）/ phase（阶段 K）；symbol=INDEX 是大盘。"""

    __tablename__ = "site_fun_stock_bar"

    symbol: str = Field(primary_key=True)
    kind: str = Field(primary_key=True)
    start_ms: int = Field(sa_column=Column(BigInteger, primary_key=True))
    open: float = Field(sa_column=Column(Float, nullable=False))
    high: float = Field(sa_column=Column(Float, nullable=False))
    low: float = Field(sa_column=Column(Float, nullable=False))
    close: float = Field(sa_column=Column(Float, nullable=False))
    volume: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))


class SiteFunStockNews(SQLModel, table=True):
    """新闻、传闻、分红公告。"""

    __tablename__ = "site_fun_stock_news"
    __table_args__ = (Index("ix_site_fun_stock_news_published", "published_at"),)

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    published_at: datetime
    kind: str
    scope: str
    symbols: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    sector: str | None = None
    direction: str = "neutral"
    strength: int = 0
    headline: str = ""
    summary: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    source_name: str | None = None
    source_url: str | None = None


class SiteFunStockAccount(SQLModel, table=True):
    """股市账户汇总（余额在钱包里）。"""

    __tablename__ = "site_fun_stock_account"

    user_id: str = Field(primary_key=True)
    realized: float = Field(default=0, sa_column=Column(Float, nullable=False))
    dividends: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    dividend_carry: float = Field(default=0, sa_column=Column(Float, nullable=False))
    """分红不足 1 类脑币的零头，攒够了再发。"""


class SiteFunStockPosition(SQLModel, table=True):
    """持仓。"""

    __tablename__ = "site_fun_stock_position"

    user_id: str = Field(primary_key=True)
    symbol: str = Field(primary_key=True)
    qty: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    avg_cost: float = Field(default=0, sa_column=Column(Float, nullable=False))


class SiteFunStockTrade(SQLModel, table=True):
    """成交流水；(user_id, client_order_id) 唯一，保证下单幂等。"""

    __tablename__ = "site_fun_stock_trade"
    __table_args__ = (
        UniqueConstraint("user_id", "client_order_id", name="uq_site_fun_stock_trade_order"),
        Index("ix_site_fun_stock_trade_user", "user_id", "id"),
    )

    id: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    user_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: int = Field(sa_column=Column(BigInteger, nullable=False))
    avg_price: float = Field(sa_column=Column(Float, nullable=False))
    gross: int = Field(sa_column=Column(BigInteger, nullable=False))
    fee: int = Field(sa_column=Column(BigInteger, nullable=False))
    total: int = Field(sa_column=Column(BigInteger, nullable=False))
    realized: int = Field(default=0, sa_column=Column(BigInteger, nullable=False))
    created_at: datetime = Field(default_factory=utc_now)
