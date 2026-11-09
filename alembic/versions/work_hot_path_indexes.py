"""Build nonunique work hot-path indexes without blocking normal table writes.

This revision intentionally uses autocommit: successful index builds survive a
later failure. A rerun accepts only exact, ready, valid definitions. Invalid or
conflicting objects are reported, never silently skipped or automatically dropped.
"""

from alembic import op
import sqlalchemy as sa

revision = "work_hot_path_indexes"
down_revision = "work_projection_write_guards"
branch_labels = None
depends_on = None

# Last item is PostgreSQL's canonical pg_get_indexdef suffix, checked on resume.
INDEXES = (
    (
        "ix_work_task_active_id",
        "work_task",
        "(id) WHERE status IN ('pending', 'running')",
        "(id) WHERE ((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying])::text[]))",
    ),
    (
        "ix_work_task_active_work_kind_id",
        "work_task",
        "(work_id, kind, id) WHERE status IN ('pending', 'running')",
        "(work_id, kind, id) WHERE ((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying])::text[]))",
    ),
    (
        "ix_work_task_active_thread_id",
        "work_task",
        "(work_id, kind, ((payload ->> 'thread_id')::varchar), id) WHERE status IN ('pending', 'running')",
        "(work_id, kind, (((payload ->> 'thread_id'::text))::character varying), id) WHERE ((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying])::text[]))",
    ),
    (
        "ix_work_visible_created_id",
        "work",
        "(created_at DESC NULLS LAST, id DESC) WHERE deleted_at IS NULL AND source_visible IS TRUE",
        "(created_at DESC NULLS LAST, id DESC) WHERE ((deleted_at IS NULL) AND (source_visible IS TRUE))",
    ),
    (
        "ix_work_author_created_id",
        "work",
        "(author_id, created_at DESC NULLS LAST, id DESC)",
        "(author_id, created_at DESC NULLS LAST, id DESC)",
    ),
    ("ix_thread_work_id", "thread", "(work_id)", "(work_id)"),
    (
        "ix_thread_follow_active_work_time",
        "thread_follow",
        "(work_id, followed_at) WHERE active_flag IS TRUE",
        "(work_id, followed_at) WHERE (active_flag IS TRUE)",
    ),
    ("ix_booklist_item_work_id", "booklist_item", "(work_id)", "(work_id)"),
    (
        "ix_work_announcement_work_created_id",
        "work_announcement",
        "(work_id, created_at DESC, id DESC)",
        "(work_id, created_at DESC, id DESC)",
    ),
)


def _verify(name, table, canonical):
    """Refuse incomplete builds or name collisions, including on retry."""
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    row = (
        bind.execute(
            sa.text("""
        SELECT i.indisvalid, i.indisready, pg_get_indexdef(c.oid) AS definition
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_index i ON i.indexrelid = c.oid
        WHERE n.nspname = :schema AND c.relname = :name
    """),
            {"schema": schema, "name": name},
        )
        .mappings()
        .first()
    )
    if row is None:
        return False
    quote = bind.dialect.identifier_preparer.quote
    expected = f"CREATE INDEX {quote(name)} ON {quote(schema)}.{quote(table)} USING btree {canonical}"
    if not row["indisvalid"] or not row["indisready"] or row["definition"] != expected:
        raise RuntimeError(
            f"Index {schema}.{name} exists but is invalid or has a different definition. "
            "Inspect pg_index/pg_get_indexdef; recover this exact object explicitly before retrying."
        )
    return True


def upgrade():
    """Build one index at a time outside the surrounding migration transaction."""
    offline = op.get_context().as_sql
    with op.get_context().autocommit_block():
        for name, table, definition, canonical in INDEXES:
            if not offline and _verify(name, table, canonical):
                continue
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {table} {definition}")
            if not offline:
                _verify(name, table, canonical)


def downgrade():
    """Remove only this revision's verified nonunique access paths."""
    offline = op.get_context().as_sql
    with op.get_context().autocommit_block():
        for name, table, _, canonical in reversed(INDEXES):
            if offline or _verify(name, table, canonical):
                op.execute(f"DROP INDEX CONCURRENTLY {name}")
