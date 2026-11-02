"""为原生 Fluxer 用户启用独立身份列，保留所有旧审计值。"""

from alembic import op
import sqlalchemy as sa

revision = "fluxer_identity_runtime"
down_revision = "fluxer_identity_history"
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
    """允许原生账户没有 Discord ID，并独立约束统一身份唯一性。"""
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 功能切换只接受全部源身份已经映射的投影，缺失时立即退出。
    for table, columns in REFERENCES.items():
        for column in columns:
            target = (
                "fluxer_user_id"
                if column in {"id", "discord_user_id"}
                else "fluxer_" + column
            )
            missing = (
                op.get_bind()
                .execute(
                    sa.text(
                        f'SELECT count(*) FROM {table} t LEFT JOIN discord_identity d ON d.discord_user_id=t.{column}::text LEFT JOIN "user" u ON u.id=t.{target} WHERE (t.{column}<>0 AND (d.user_id IS NULL OR t.{target} IS DISTINCT FROM d.user_id)) OR (t.{target} IS NOT NULL AND u.id IS NULL)'
                    )
                )
                .scalar_one()
            )
            if missing:
                raise RuntimeError(
                    f"{table}.{column} 仍有未完成的身份映射，请先回填并审计"
                )
    inspector = sa.inspect(op.get_bind())
    # 投票原复合主键包含 Discord 身份，改用实体代理主键后才允许原生用户。
    vote_pk = inspector.get_pk_constraint("tag_vote")
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM tag_vote WHERE id IS NULL"))
        .scalar_one()
    ):
        raise RuntimeError("请先完成在线投票键回填与审计")
    op.drop_constraint(vote_pk["name"], "tag_vote", type_="primary")
    op.create_primary_key("pk_tag_vote_id", "tag_vote", ["id"])
    op.create_unique_constraint(
        "uq_tag_vote_legacy", "tag_vote", ["binding_id", "user_id"]
    )
    inspector = sa.inspect(op.get_bind())
    unique_indexes = []
    for table, columns in REFERENCES.items():
        for column in columns:
            if not (table == "author" and column in {"id", "discord_user_id"}):
                op.alter_column(table, column, nullable=True)
        candidates = inspector.get_unique_constraints(table) + [
            item for item in inspector.get_indexes(table) if item.get("unique")
        ]
        # 同一统一用户可以有多个 Discord 历史作者投影。
        seen = set()
        for candidate in candidates:
            original = candidate["column_names"]
            if not original or not any(column in columns for column in original):
                continue
            canonical = tuple(
                (
                    "fluxer_user_id"
                    if column in {"id", "discord_user_id"}
                    else "fluxer_" + column
                )
                if column in columns
                else column
                for column in original
            )
            if canonical in seen:
                continue
            seen.add(canonical)
            unique_indexes.append((table, list(canonical)))
    op.create_check_constraint(
        "ck_fluxer_author_follow_not_self",
        "author_follow",
        "fluxer_user_id <> fluxer_author_id",
    )
    # 并发建索引不阻断仍在工作的旧系统写入。
    with op.get_context().autocommit_block():
        for table, columns in REFERENCES.items():
            for column in columns:
                target = (
                    "fluxer_user_id"
                    if column in {"id", "discord_user_id"}
                    else "fluxer_" + column
                )
                op.create_index(
                    f"ix_{table}_{target}",
                    table,
                    [target],
                    postgresql_concurrently=True,
                )
        for number, (table, columns) in enumerate(unique_indexes):
            op.create_index(
                f"ux_fluxer_{table}_{number}",
                table,
                columns,
                unique=True,
                postgresql_concurrently=True,
            )


def downgrade():
    """原生用户没有可逆的 Discord 身份，不允许自动回退。"""
    raise RuntimeError("Fluxer 身份运行迁移不可自动回退")
