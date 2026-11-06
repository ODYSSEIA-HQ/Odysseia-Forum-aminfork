"""协议 /api/v1 层（site_v1）一次性建好后面各领域要用的表和字段。

- 账号：site_account、site_user_prefs、site_restricted_access、site_user_block、
  site_fake_credential（只在 FLUXER_FAKE=1 时有数据）、site_setting；
- 作品：work 加 status、summary、layout、封面/图库附件、declarations、rating_lock、
  moderation_state、tag_locked、published_at、updated_at、visibility，现有作品回填为 published；
  work_statistics 加 download_count；work_version、work_version_file；
- 附件：attachment；
- tag 目录：site_tag_group、site_tag、site_tag_question、site_work_tag；
- 举报审核：report、moderation_queue_item、user_moderation_record。

新表都不挂 thread/booklist 投影触发器。work 的新列都带数据库默认值，
project_thread_work 触发器按列名插入 work 时由默认值补齐。
"""

from alembic import op
import sqlalchemy as sa

revision = "site_v1_schema"
down_revision = "forum_nsfw_sync_state"
branch_labels = None
depends_on = None

NEW_TABLES = (
    "user_moderation_record",
    "moderation_queue_item",
    "report",
    "site_work_tag",
    "site_tag_question",
    "site_tag",
    "site_tag_group",
    "attachment",
    "work_version_file",
    "work_version",
    "site_setting",
    "site_fake_credential",
    "site_user_block",
    "site_restricted_access",
    "site_user_prefs",
    "site_account",
)

WORK_COLUMNS = (
    "visibility",
    "updated_at",
    "published_at",
    "tag_locked",
    "moderation_state",
    "rating_lock",
    "declarations",
    "gallery_attachment_ids",
    "cover_attachment_id",
    "layout",
    "summary",
    "status",
)


def _id():
    return sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True)


def upgrade():
    """建表、加列、回填；全部在一个事务里，锁等待超过 5 秒直接失败。"""
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ---------------- 账号 ----------------
    op.create_table(
        "site_account",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("newcomer_until", sa.DateTime(), nullable=True),
        sa.Column(
            "quiz_passed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "site_user_prefs",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column(
            "content_mode", sa.String(), nullable=False, server_default="general"
        ),
        sa.Column("show_r18g", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("blur_r18", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("blocked_tag_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("followed_tag_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("dm_privacy", sa.String(), nullable=False, server_default="everyone"),
        sa.Column("pinned_boards", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "site_restricted_access",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("state", sa.String(), nullable=False, server_default="none"),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("age_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("terms_version", sa.String(), nullable=True),
        sa.Column("revoked_by", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "site_user_block",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("blocked_user_id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_site_user_block_blocked", "site_user_block", ["blocked_user_id"]
    )
    op.create_table(
        "site_fake_credential",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "uq_site_fake_credential_username",
        "site_fake_credential",
        [sa.text("lower(username)")],
        unique=True,
    )
    op.create_index(
        "uq_site_fake_credential_email",
        "site_fake_credential",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_table(
        "site_setting",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ---------------- 作品 ----------------
    op.add_column(
        "work",
        sa.Column("status", sa.String(), nullable=False, server_default="published"),
    )
    op.add_column(
        "work", sa.Column("summary", sa.String(), nullable=False, server_default="")
    )
    op.add_column("work", sa.Column("layout", sa.JSON(), nullable=True))
    op.add_column(
        "work", sa.Column("cover_attachment_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "work",
        sa.Column(
            "gallery_attachment_ids", sa.JSON(), nullable=False, server_default="[]"
        ),
    )
    op.add_column(
        "work",
        sa.Column("declarations", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column("work", sa.Column("rating_lock", sa.JSON(), nullable=True))
    op.add_column(
        "work",
        sa.Column("moderation_state", sa.String(), nullable=False, server_default="ok"),
    )
    op.add_column(
        "work",
        sa.Column(
            "tag_locked", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("work", sa.Column("published_at", sa.DateTime(), nullable=True))
    op.add_column(
        "work",
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column(
        "work",
        sa.Column("visibility", sa.String(), nullable=False, server_default="public"),
    )
    # 现有作品（V2 原生、DC 迁移）都是已发布；时间按已有字段回填，分级受限的标 restricted。
    op.execute(
        """
        UPDATE work SET
          status = 'published',
          published_at = created_at,
          updated_at = coalesce(last_active_at, created_at),
          visibility = CASE WHEN is_nsfw OR is_abyss THEN 'restricted' ELSE 'public' END
        """
    )
    op.create_index("ix_work_status", "work", ["status"])
    op.create_index("ix_work_visibility", "work", ["visibility"])

    op.add_column(
        "work_statistics",
        sa.Column(
            "download_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )

    op.create_table(
        "work_version",
        _id(),
        sa.Column("work_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=False, server_default=""),
        sa.Column("license", sa.Text(), nullable=False, server_default=""),
        sa.Column("dependencies", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("work_id", "name", name="uq_work_version_name"),
    )
    op.create_index("ix_work_version_work_id", "work_version", ["work_id"])
    op.create_index("ix_work_version_created_at", "work_version", ["created_at"])
    op.create_table(
        "work_version_file",
        sa.Column("version_id", sa.BigInteger(), primary_key=True),
        sa.Column("attachment_id", sa.BigInteger(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
    )

    # ---------------- 附件 ----------------
    op.create_table(
        "attachment",
        _id(),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("work_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False, unique=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("rating", sa.String(), nullable=True),
        sa.Column("auto_rating", sa.String(), nullable=True),
        sa.Column("model_scores", sa.JSON(), nullable=True),
        sa.Column("review_state", sa.String(), nullable=False),
        sa.Column("reject_reason", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_attachment_state", "attachment", ["state"])
    op.create_index(
        "ix_attachment_owner_created", "attachment", ["owner_id", "created_at"]
    )
    op.create_index(
        "ix_attachment_work_purpose",
        "attachment",
        ["work_id", "purpose"],
        postgresql_where=sa.text("work_id IS NOT NULL AND deleted_at IS NULL"),
    )

    # ---------------- tag 目录 ----------------
    op.create_table(
        "site_tag_group",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("max_select", sa.Integer(), nullable=False),
        sa.Column("only_for_types", sa.JSON(), nullable=True),
        sa.Column("required_for_types", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_table(
        "site_tag",
        _id(),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("restricts", sa.Boolean(), nullable=False),
        sa.Column("rating", sa.String(), nullable=True),
        sa.Column("official", sa.Boolean(), nullable=False),
        sa.Column("for_types", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_site_tag_group_id", "site_tag", ["group_id"])
    op.create_index(
        "uq_site_tag_group_name",
        "site_tag",
        ["group_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "site_tag_question",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("hint", sa.Text(), nullable=False, server_default=""),
        sa.Column("for_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("restricts_tag_id", sa.BigInteger(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_table(
        "site_work_tag",
        _id(),
        sa.Column("work_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.Column("removed_by", sa.String(), nullable=True),
    )
    op.create_index("ix_site_work_tag_work_id", "site_work_tag", ["work_id"])
    op.create_index(
        "uq_site_work_tag_active",
        "site_work_tag",
        ["work_id", "tag_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_index(
        "ix_site_work_tag_reverse",
        "site_work_tag",
        ["tag_id", "work_id"],
        postgresql_where=sa.text("removed_at IS NULL"),
    )

    # ---------------- 举报审核 ----------------
    op.create_table(
        "report",
        _id(),
        sa.Column("reporter_id", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("queue_item_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "uq_report_pending_reporter_target",
        "report",
        ["reporter_id", "target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index("ix_report_queue_item", "report", ["queue_item_id"])
    op.create_table(
        "moderation_queue_item",
        _id(),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=True),
        sa.Column("work_id", sa.BigInteger(), nullable=True),
        sa.Column("subject_user_id", sa.String(), nullable=True),
        sa.Column("auto_rating", sa.String(), nullable=True),
        sa.Column("model_scores", sa.JSON(), nullable=True),
        sa.Column("usage", sa.String(), nullable=True),
        sa.Column("appealed", sa.Boolean(), nullable=False),
        sa.Column("appeal_note", sa.String(), nullable=True),
        sa.Column("target_snapshot", sa.JSON(), nullable=True),
        sa.Column("resolution", sa.JSON(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_moderation_queue_item_subject_user_id",
        "moderation_queue_item",
        ["subject_user_id"],
    )
    op.create_index(
        "uq_moderation_queue_pending_target",
        "moderation_queue_item",
        ["type", "target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_moderation_queue_state_created",
        "moderation_queue_item",
        ["state", "type", "created_at"],
    )
    op.create_table(
        "user_moderation_record",
        _id(),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("until", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("strike", sa.Boolean(), nullable=False),
        sa.Column("work_id", sa.BigInteger(), nullable=True),
        sa.Column("work_title", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("by_user_id", sa.String(), nullable=False),
        sa.Column("queue_item_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_user_moderation_record_user_created",
        "user_moderation_record",
        ["user_id", "created_at"],
    )


def downgrade():
    """新表和新列只被 /api/v1 使用，可以整体移除；V1/V2 数据不受影响。"""
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in NEW_TABLES:
        op.drop_table(table)
    op.drop_column("work_statistics", "download_count")
    op.drop_index("ix_work_visibility", table_name="work")
    op.drop_index("ix_work_status", table_name="work")
    for column in WORK_COLUMNS:
        op.drop_column("work", column)
