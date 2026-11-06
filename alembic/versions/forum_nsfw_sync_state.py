"""Add empty NSFW sync version guard, without migrating existing user choices."""

from alembic import op
import sqlalchemy as sa

revision = "forum_nsfw_sync_state"
down_revision = "work_banner_extension"
branch_labels = None
depends_on = None


def upgrade():
    """创建按用户与社区保存单调版本的同步状态表。"""
    op.create_table(
        "forum_nsfw_sync_state",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("nsfw_version", sa.BigInteger(), nullable=False),
        sa.Column("show_nsfw", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "guild_id"),
    )


def downgrade():
    """移除同步状态表，用户原有展示开关保持不变。"""
    raise RuntimeError(
        "Dropping the sync version guard could replay stale choices; plan a coordinated rollback"
    )
