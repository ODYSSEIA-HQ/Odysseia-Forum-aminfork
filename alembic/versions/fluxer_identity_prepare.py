"""在线准备：只加投影列，保留旧身份和旧写入行为。"""

from alembic import op
import sqlalchemy as sa

revision = "fluxer_identity_prepare"
down_revision = "add_channel_metadata"
branch_labels = None
depends_on = None

REFERENCES = {
    "author": ["id"],
    "thread": ["author_id"],
    "booklist": ["owner_id"],
    "booklist_item": ["owner_id"],
    "booklist_publish": ["discord_user_id"],
    "author_follow": ["user_id", "author_id"],
    "thread_follow": ["user_id"],
    "user_collection": ["user_id"],
    "user_search_preferences": ["user_id"],
    "user_update_preference": ["user_id"],
    "notification": ["user_id"],
    "thread_update": ["publisher_id"],
    "banner_application": ["applicant_id", "reviewer_id"],
    "banner_waitlist": ["applicant_id"],
    "banner_carousel": ["applicant_id"],
    "bot_config": ["update_user_id"],
    "tag_binding": ["actor_id"],
    "tag_vote": ["user_id"],
    "tag_proposal": ["applicant_id", "owner_id"],
    "operation_log": ["actor_id"],
}


def upgrade():
    """添加本地投影与旧写入捕获触发器，不运行用户预建或历史回填。"""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        'CREATE TABLE "user" (id VARCHAR PRIMARY KEY, username VARCHAR NOT NULL, display_name VARCHAR, avatar_url VARCHAR, updated_at TIMESTAMP NOT NULL DEFAULT now())'
    )
    op.execute(
        "CREATE TABLE discord_identity (discord_user_id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL UNIQUE, version BIGINT NOT NULL DEFAULT 1)"
    )
    # 投票代理键先只添加可空列和新行默认值，历史行由分批 CLI 填充。
    op.add_column("tag_vote", sa.Column("id", sa.BigInteger(), nullable=True))
    op.execute("CREATE SEQUENCE fluxer_tag_vote_id_seq OWNED BY tag_vote.id")
    op.execute("ALTER TABLE tag_vote ALTER COLUMN id SET DEFAULT nextval('fluxer_tag_vote_id_seq')")
    # 删除已有实体外键，历史迁移不改写。
    for table in sa.inspect(op.get_bind()).get_table_names():
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table):
            op.drop_constraint(constraint["name"], table, type_="foreignkey")
    for table, columns in REFERENCES.items():
        assignments = []
        for column in columns:
            target = "fluxer_user_id" if column in {"id", "discord_user_id"} else "fluxer_" + column
            op.add_column(table, sa.Column(target, sa.String(), nullable=True))
            assignments.append(
                f"IF (TG_OP = 'INSERT' AND NEW.{target} IS NULL) OR (TG_OP = 'UPDATE' AND NEW.{column} IS DISTINCT FROM OLD.{column} AND NEW.{target} IS NOT DISTINCT FROM OLD.{target}) THEN NEW.{target} := (SELECT user_id FROM discord_identity WHERE discord_user_id = NEW.{column}::text AND NEW.{column} <> 0); END IF;"
            )
        # 同一语句内捕获旧进程的身份变化；缺映射保留 NULL，由审计阻止切换。
        body = " ".join(assignments)
        op.execute(
            f"CREATE FUNCTION capture_fluxer_{table}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {body} RETURN NEW; END $$"
        )
        op.execute(
            f"CREATE TRIGGER capture_fluxer_{table} BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION capture_fluxer_{table}()"
        )
    for column in ("include_authors", "exclude_authors"):
        op.add_column(
            "user_search_preferences",
            sa.Column("fluxer_" + column, sa.JSON(), nullable=True),
        )

    # 作者筛选列表变化时清除旧投影，回填命令可安全重建。
    op.execute(
        "CREATE FUNCTION invalidate_fluxer_author_lists() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.include_authors::jsonb IS DISTINCT FROM OLD.include_authors::jsonb THEN NEW.fluxer_include_authors := NULL; END IF; IF NEW.exclude_authors::jsonb IS DISTINCT FROM OLD.exclude_authors::jsonb THEN NEW.fluxer_exclude_authors := NULL; END IF; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER invalidate_fluxer_author_lists BEFORE UPDATE ON user_search_preferences FOR EACH ROW EXECUTE FUNCTION invalidate_fluxer_author_lists()"
    )


def downgrade():
    """身份迁移包含审计数据，只允许人工确认后的前向修复。"""
    raise RuntimeError("Fluxer 在线准备迁移不可自动回退")
