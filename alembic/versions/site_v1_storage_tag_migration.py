"""旧标签迁成协议 tag 的迁移记录表（协议 7.3，scripts/migrate_legacy_tags.py）；
附件按 sha256 找「复核过的同一张图」的部分索引（services/attachments.reviewed_by_hash）。

表结构以 models/site_tag_migration.py 为准。
"""

from alembic import op

revision = "site_v1_storage_tag_migration"
down_revision = "site_v1_me_tables"
branch_labels = None
depends_on = None


def upgrade():
    from models.site_tag_migration import SiteTagMigrationRecord

    op.execute("SET LOCAL lock_timeout = '5s'")
    SiteTagMigrationRecord.__table__.create(bind=op.get_bind(), checkfirst=False)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_attachment_sha256_reviewed ON attachment (sha256) "
        "WHERE review_state = 'reviewed' AND sha256 IS NOT NULL"
    )


def downgrade():
    from models.site_tag_migration import SiteTagMigrationRecord

    op.execute("DROP INDEX IF EXISTS ix_attachment_sha256_reviewed")
    SiteTagMigrationRecord.__table__.drop(bind=op.get_bind(), checkfirst=True)
