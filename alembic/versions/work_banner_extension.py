"""Extend existing banner facts to native Work targets without fabricated Discord IDs."""

from alembic import op
import sqlalchemy as sa

revision = "work_banner_extension"
down_revision = "work_layer_prepare"
branch_labels = None
depends_on = None


def upgrade():
    """扩展真实申请、轮播和 FIFO 候补，不建立第二套事实表。"""
    for table in ("banner_application", "banner_carousel", "banner_waitlist"):
        op.add_column(table, sa.Column("work_id", sa.BigInteger(), nullable=True))
        op.add_column(table, sa.Column("fluxer_owner_id", sa.String(), nullable=True))
        op.create_index(f"ix_{table}_work_id", table, ["work_id"])
        op.create_index(f"ix_{table}_fluxer_owner_id", table, ["fluxer_owner_id"])
        op.alter_column(
            table, "thread_id", existing_type=sa.BigInteger(), nullable=True
        )
        op.execute(
            sa.text(f"""
            UPDATE {table} b SET work_id=t.work_id
            FROM thread t WHERE b.target_type=1 AND t.platform='discord'
              AND b.thread_id=t.thread_id AND t.work_id IS NOT NULL
        """)
        )
        op.execute(
            sa.text(f"""
            UPDATE {table} SET fluxer_owner_id=COALESCE(
                canonical_forum_user(fluxer_applicant_id),
                (SELECT user_id FROM discord_identity
                 WHERE discord_user_id={table}.applicant_id::text)
            )
        """)
        )
        op.create_check_constraint(
            f"ck_{table}_work_target",
            table,
            "(target_type=3 AND work_id IS NOT NULL AND thread_id IS NULL) OR "
            "(target_type IN (1,2) AND thread_id IS NOT NULL)",
        )
    op.alter_column(
        "banner_application", "channel_id", existing_type=sa.BigInteger(), nullable=True
    )


def downgrade():
    """原生作品事实不能无损退回旧协议，阻止破坏性降级。"""
    raise RuntimeError("原生作品 Banner 需要保留；请先迁移目标再规划降级")
