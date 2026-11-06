"""/api/v1 找作品一块（nav）：tag 同义词、禁用记录、公共聊天区频道、横幅申请的协议字段。

首页栏位、精选和推广位配置存在 site_setting（home.rails、home.featured），不建表。
"""

from alembic import op
import sqlalchemy as sa

revision = "site_v1_nav_schema"
down_revision = "site_v1_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "site_tag_alias",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_lower", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_site_tag_alias_tag_id", "site_tag_alias", ["tag_id"])
    op.create_index(
        "uq_site_tag_alias_name", "site_tag_alias", ["name_lower"], unique=True
    )

    op.create_table(
        "site_tag_disabled",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_lower", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("disabled_by", sa.String(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_site_tag_disabled_tag_id", "site_tag_disabled", ["tag_id"])
    op.create_index(
        "uq_site_tag_disabled_name", "site_tag_disabled", ["name_lower"], unique=True
    )

    op.create_table(
        "site_public_channel",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("rating", sa.String(), nullable=False, server_default="general"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rules", sa.Text(), nullable=False, server_default=""),
        sa.Column("fluxer_channel_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_site_public_channel_fluxer_channel_id",
        "site_public_channel",
        ["fluxer_channel_id"],
    )

    op.create_table(
        "site_banner_meta",
        sa.Column("application_id", sa.Integer(), primary_key=True),
        sa.Column("cover_attachment_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("start_at", sa.DateTime(), nullable=True),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column("carousel_id", sa.Integer(), nullable=True),
    )


def downgrade():
    for table in (
        "site_banner_meta",
        "site_public_channel",
        "site_tag_disabled",
        "site_tag_alias",
    ):
        op.drop_table(table)
