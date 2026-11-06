"""/api/v1 活动（3.22）和娱乐区（/fun）的表。

- 活动：site_event、site_event_entry、site_event_follow、site_event_log；
- 娱乐区：site_fun_wallet（类脑币）、site_fun_bj_round / site_fun_bj_shoe（21 点）、
  site_fun_market（股市状态，单行）、site_fun_stock_bar（K 线）、site_fun_stock_news、
  site_fun_stock_account / site_fun_stock_position / site_fun_stock_trade（持仓和成交）。

都是新表，不挂投影触发器，不改已有表。
"""

from alembic import op
import sqlalchemy as sa

revision = "site_v1_events_schema"
down_revision = "site_v1_schema"
branch_labels = None
depends_on = None

TABLES = (
    "site_fun_stock_trade",
    "site_fun_stock_position",
    "site_fun_stock_account",
    "site_fun_stock_news",
    "site_fun_stock_bar",
    "site_fun_market",
    "site_fun_bj_shoe",
    "site_fun_bj_round",
    "site_fun_wallet",
    "site_event_log",
    "site_event_follow",
    "site_event_entry",
    "site_event",
)


def _id():
    return sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ---------------- 活动 ----------------
    op.create_table(
        "site_event",
        _id(),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("rules", sa.Text(), nullable=False, server_default=""),
        sa.Column("cover_attachment_id", sa.BigInteger(), nullable=True),
        sa.Column("audience", sa.String(), nullable=False, server_default="all"),
        sa.Column("manual_state", sa.String(), nullable=False, server_default="draft"),
        sa.Column("submit_start_at", sa.DateTime(), nullable=False),
        sa.Column("submit_end_at", sa.DateTime(), nullable=False),
        sa.Column("result_at", sa.DateTime(), nullable=True),
        sa.Column("eligibility", sa.JSON(), nullable=False),
        sa.Column("divisions", sa.JSON(), nullable=False),
        sa.Column("awards", sa.JSON(), nullable=False),
        sa.Column("organizer_ids", sa.JSON(), nullable=False),
        sa.Column("results_draft", sa.JSON(), nullable=False),
        sa.Column("results_published_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_site_event_slug", "site_event", ["slug"], unique=True)
    op.create_table(
        "site_event_entry",
        _id(),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("division_id", sa.String(), nullable=False),
        sa.Column("work_id", sa.BigInteger(), nullable=False),
        sa.Column("submitted_by", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("version_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="accepted"),
        sa.Column("state_reason", sa.String(), nullable=True),
        sa.Column("award_id", sa.String(), nullable=True),
        sa.Column("award_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_site_event_entry_event", "site_event_entry", ["event_id", "state"])
    op.create_index("ix_site_event_entry_user", "site_event_entry", ["submitted_by"])
    op.create_index("ix_site_event_entry_work", "site_event_entry", ["work_id"])
    op.create_table(
        "site_event_follow",
        sa.Column("event_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "site_event_log",
        _id(),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("work_id", sa.BigInteger(), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_site_event_log_event", "site_event_log", ["event_id", "at"])

    # ---------------- 娱乐区 ----------------
    op.create_table(
        "site_fun_wallet",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.Column("last_daily_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "site_fun_bj_round",
        _id(),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("bet", sa.BigInteger(), nullable=False),
        sa.Column("doubled", sa.Boolean(), nullable=False),
        sa.Column("player", sa.JSON(), nullable=False),
        sa.Column("dealer", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("payout", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_site_fun_bj_round_user_state", "site_fun_bj_round", ["user_id", "state"]
    )
    op.create_table(
        "site_fun_bj_shoe",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("cards", sa.JSON(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
    )
    op.create_table(
        "site_fun_market",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "site_fun_stock_bar",
        sa.Column("symbol", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("start_ms", sa.BigInteger(), primary_key=True),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "site_fun_stock_news",
        _id(),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("sector", sa.String(), nullable=True),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("strength", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_name", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_site_fun_stock_news_published", "site_fun_stock_news", ["published_at"]
    )
    op.create_table(
        "site_fun_stock_account",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("realized", sa.Float(), nullable=False),
        sa.Column("dividends", sa.BigInteger(), nullable=False),
        sa.Column("dividend_carry", sa.Float(), nullable=False),
    )
    op.create_table(
        "site_fun_stock_position",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("symbol", sa.String(), primary_key=True),
        sa.Column("qty", sa.BigInteger(), nullable=False),
        sa.Column("avg_cost", sa.Float(), nullable=False),
    )
    op.create_table(
        "site_fun_stock_trade",
        _id(),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("client_order_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("qty", sa.BigInteger(), nullable=False),
        sa.Column("avg_price", sa.Float(), nullable=False),
        sa.Column("gross", sa.BigInteger(), nullable=False),
        sa.Column("fee", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column("realized", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id", "client_order_id", name="uq_site_fun_stock_trade_order"
        ),
    )
    op.create_index(
        "ix_site_fun_stock_trade_user", "site_fun_stock_trade", ["user_id", "id"]
    )


def downgrade():
    for table in TABLES:
        op.drop_table(table)
