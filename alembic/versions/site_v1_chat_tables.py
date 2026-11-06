"""/api/v1 聊天（代转 Fluxer 消息）的表。

- site_chat_message_meta：经本站发出的消息挂的本站附件、所属目标、删除记录；
- site_chat_read_state：已读位置（GET .../state、未读数）；
- site_chat_message、site_chat_dm_channel：假 Fluxer（FLUXER_FAKE=1）的消息和私信频道，
  真实环境为空。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "site_v1_chat_tables"
down_revision = "site_v1_events_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_chat_message_meta",
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.String(64)),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column(
            "attachment_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("deleted_state", sa.String(16)),
        sa.Column("deleted_by", sa.String(64)),
        sa.Column("deleted_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "site_chat_read_state",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("last_read_message_id", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_site_chat_read_state_target",
        "site_chat_read_state",
        ["target_kind", "target_id"],
    )
    op.create_table(
        "site_chat_message",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("author_id", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("reply_to", sa.BigInteger()),
        sa.Column("nonce", sa.String(64)),
        sa.Column(
            "reactions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("edited_at", sa.DateTime()),
        sa.Column("deleted_state", sa.String(16)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_site_chat_message_channel_id", "site_chat_message", ["channel_id", "id"]
    )
    op.create_table(
        "site_chat_dm_channel",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_low", sa.String(64), nullable=False),
        sa.Column("user_high", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_low", "user_high", name="uq_site_chat_dm_pair"),
    )
    op.create_index(
        "ix_site_chat_dm_channel_user_high", "site_chat_dm_channel", ["user_high"]
    )


def downgrade() -> None:
    op.drop_table("site_chat_dm_channel")
    op.drop_table("site_chat_message")
    op.drop_table("site_chat_read_state")
    op.drop_table("site_chat_message_meta")
