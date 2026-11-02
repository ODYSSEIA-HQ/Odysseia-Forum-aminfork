"""在线历史身份约束，支持检查通过后的安全回退。"""

from alembic import op
import sqlalchemy as sa

revision = "fluxer_identity_history"
down_revision = "fluxer_identity_prepare"
branch_labels = None
depends_on = None


def upgrade():
    """添加历史绑定与合并链列，不改写原始身份数据。"""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "discord_identity",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("user", sa.Column("merged_into_id", sa.String(), nullable=True))
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints(
        "discord_identity"
    ):
        if constraint["column_names"] == ["user_id"]:
            op.drop_constraint(constraint["name"], "discord_identity", type_="unique")
    op.create_index(
        "uq_discord_identity_current_user",
        "discord_identity",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.execute("""CREATE FUNCTION canonical_forum_user(identifier VARCHAR) RETURNS VARCHAR LANGUAGE SQL STABLE AS $$
      WITH RECURSIVE chain AS (
        SELECT id,merged_into_id,ARRAY[id] AS path FROM "user" WHERE id=identifier
        UNION ALL SELECT u.id,u.merged_into_id,c.path||u.id FROM chain c JOIN "user" u ON u.id=c.merged_into_id
        WHERE NOT u.id=ANY(c.path) AND cardinality(c.path)<64
      ) SELECT id FROM chain WHERE merged_into_id IS NULL LIMIT 1
    $$""")


def downgrade():
    """只有未发生合并或多历史绑定时还原旧约束，避免丢失身份历史。"""
    connection = op.get_bind()
    unsafe = connection.execute(
        sa.text(
            'SELECT EXISTS(SELECT 1 FROM "user" WHERE merged_into_id IS NOT NULL) OR EXISTS(SELECT 1 FROM discord_identity GROUP BY user_id HAVING count(*)>1) OR EXISTS(SELECT 1 FROM discord_identity WHERE NOT is_current)'
        )
    ).scalar_one()
    if unsafe:
        raise RuntimeError(
            "已有历史身份或合并数据，请保留历史并使用前向修复；本次回退未修改任何数据"
        )
    op.drop_index("uq_discord_identity_current_user", table_name="discord_identity")
    op.create_unique_constraint(
        "discord_identity_user_id_key", "discord_identity", ["user_id"]
    )
    op.execute("DROP FUNCTION canonical_forum_user(VARCHAR)")
    op.drop_column("discord_identity", "is_current")
    op.drop_column("user", "merged_into_id")
