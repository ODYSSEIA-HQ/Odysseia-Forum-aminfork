"""Avoid projection writes for child exposure and collection metadata updates."""

from alembic import op

revision = "work_projection_write_guards"
down_revision = "forum_nsfw_sync_state"
branch_labels = None
depends_on = None

PROJECT_PREVIOUS = """CREATE OR REPLACE FUNCTION project_thread_work() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.work_id IS NULL THEN
        INSERT INTO work(title,description,images,search_vector,last_active_at,author_id,source_platform,source_guild_id,source_channel_id,source_thread_id,main_ref,created_at,source_visible,deleted_at,deletion_reason)
        VALUES(NEW.title,coalesce(nullif(NEW.body,''),NEW.first_message_excerpt),coalesce(NEW.thumbnail_urls,'[]'::json),NEW.search_vector,NEW.last_active_at,NEW.fluxer_author_id,NEW.platform,NEW.guild_id::text,NEW.channel_id::text,NEW.thread_id::text,
          CASE WHEN NEW.platform='discord' THEN 'https://discord.com/channels/'||NEW.guild_id||'/'||NEW.thread_id ELSE '' END,NEW.created_at,NEW.show_flag,NEW.deleted_at,NEW.deletion_reason) RETURNING id INTO NEW.work_id;
      ELSIF NEW.role='main' THEN
        UPDATE work SET title=CASE WHEN work.metadata_overrides::jsonb ? 'title' THEN work.title ELSE NEW.title END, description=CASE WHEN work.metadata_overrides::jsonb ? 'description' THEN work.description ELSE coalesce(nullif(NEW.body,''),NEW.first_message_excerpt) END, images=CASE WHEN work.metadata_overrides::jsonb ? 'images' THEN work.images ELSE coalesce(NEW.thumbnail_urls,'[]'::json) END,author_id=NEW.fluxer_author_id,
          source_visible=NEW.show_flag,source_channel_id=NEW.channel_id::text,search_vector=CASE WHEN work.metadata_overrides::jsonb ?| ARRAY['title','description'] THEN work.search_vector ELSE NEW.search_vector END,last_active_at=greatest(work.last_active_at,NEW.last_active_at) WHERE id=NEW.work_id AND deleted_at IS NULL;
      END IF;
      IF NEW.role='main' THEN
        INSERT INTO work_statistics(work_id,display_count,reaction_count,reply_count) VALUES(NEW.work_id,greatest(NEW.display_count,0),greatest(NEW.reaction_count,0),greatest(NEW.reply_count,0))
        ON CONFLICT(work_id) DO UPDATE SET display_count=greatest(work_statistics.display_count,excluded.display_count),reaction_count=excluded.reaction_count,reply_count=excluded.reply_count;
      ELSE
        INSERT INTO work_statistics(work_id) VALUES(NEW.work_id) ON CONFLICT(work_id) DO NOTHING;
      END IF;
      INSERT INTO work_task(work_id,kind,dedupe_key,payload) VALUES(NEW.work_id,'projection','thread:'||NEW.id||':'||txid_current(),json_build_object('thread_id',NEW.id)) ON CONFLICT(dedupe_key) DO NOTHING;
      RETURN NEW;
    END $$"""

COLLECTION_PREVIOUS = """CREATE OR REPLACE FUNCTION refresh_work_collections() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE identifier BIGINT; previous BIGINT; following BIGINT;
    BEGIN
      IF TG_OP<>'INSERT' THEN previous:=OLD.work_id; END IF;
      IF TG_OP<>'DELETE' THEN following:=NEW.work_id; END IF;
      FOR identifier IN SELECT DISTINCT value FROM unnest(ARRAY[previous,following]) AS ids(value) WHERE value IS NOT NULL ORDER BY value LOOP
        PERFORM pg_advisory_xact_lock(identifier);
        INSERT INTO work_statistics(work_id,collection_count) SELECT identifier,count(DISTINCT coalesce(b.fluxer_owner_id,b.owner_id::text)) FROM booklist_item i JOIN booklist b ON b.id=i.booklist_id WHERE i.work_id=identifier AND b.is_default
        ON CONFLICT(work_id) DO UPDATE SET collection_count=excluded.collection_count;
      END LOOP;
      RETURN NULL;
    END $$"""

PROJECT_GUARDED = """CREATE OR REPLACE FUNCTION project_thread_work() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      -- Only an already-linked child's exposure/no-op update can skip projection.
      -- Comparing the rest of the row preserves future fields and mixed updates.
      IF TG_OP = 'UPDATE' AND NEW.role = 'child' AND NEW.work_id IS NOT NULL
         AND (to_jsonb(NEW) - 'display_count') IS NOT DISTINCT FROM
             (to_jsonb(OLD) - 'display_count') THEN
        RETURN NEW;
      END IF;
      IF NEW.work_id IS NULL THEN
        INSERT INTO work(title,description,images,search_vector,last_active_at,author_id,source_platform,source_guild_id,source_channel_id,source_thread_id,main_ref,created_at,source_visible,deleted_at,deletion_reason)
        VALUES(NEW.title,coalesce(nullif(NEW.body,''),NEW.first_message_excerpt),coalesce(NEW.thumbnail_urls,'[]'::json),NEW.search_vector,NEW.last_active_at,NEW.fluxer_author_id,NEW.platform,NEW.guild_id::text,NEW.channel_id::text,NEW.thread_id::text,
          CASE WHEN NEW.platform='discord' THEN 'https://discord.com/channels/'||NEW.guild_id||'/'||NEW.thread_id ELSE '' END,NEW.created_at,NEW.show_flag,NEW.deleted_at,NEW.deletion_reason) RETURNING id INTO NEW.work_id;
      ELSIF NEW.role='main' THEN
        UPDATE work SET title=CASE WHEN work.metadata_overrides::jsonb ? 'title' THEN work.title ELSE NEW.title END, description=CASE WHEN work.metadata_overrides::jsonb ? 'description' THEN work.description ELSE coalesce(nullif(NEW.body,''),NEW.first_message_excerpt) END, images=CASE WHEN work.metadata_overrides::jsonb ? 'images' THEN work.images ELSE coalesce(NEW.thumbnail_urls,'[]'::json) END,author_id=NEW.fluxer_author_id,
          source_visible=NEW.show_flag,source_channel_id=NEW.channel_id::text,search_vector=CASE WHEN work.metadata_overrides::jsonb ?| ARRAY['title','description'] THEN work.search_vector ELSE NEW.search_vector END,last_active_at=greatest(work.last_active_at,NEW.last_active_at) WHERE id=NEW.work_id AND deleted_at IS NULL;
      END IF;
      IF NEW.role='main' THEN
        INSERT INTO work_statistics(work_id,display_count,reaction_count,reply_count) VALUES(NEW.work_id,greatest(NEW.display_count,0),greatest(NEW.reaction_count,0),greatest(NEW.reply_count,0))
        ON CONFLICT(work_id) DO UPDATE SET display_count=greatest(work_statistics.display_count,excluded.display_count),reaction_count=excluded.reaction_count,reply_count=excluded.reply_count;
      ELSE
        INSERT INTO work_statistics(work_id) VALUES(NEW.work_id) ON CONFLICT(work_id) DO NOTHING;
      END IF;
      INSERT INTO work_task(work_id,kind,dedupe_key,payload) VALUES(NEW.work_id,'projection','thread:'||NEW.id||':'||txid_current(),json_build_object('thread_id',NEW.id)) ON CONFLICT(dedupe_key) DO NOTHING;
      RETURN NEW;
    END $$"""

COLLECTION_GUARDED = """CREATE OR REPLACE FUNCTION refresh_work_collections() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE identifier BIGINT; previous BIGINT; following BIGINT;
    BEGIN
      IF TG_OP = 'UPDATE'
         AND NEW.work_id IS NOT DISTINCT FROM OLD.work_id
         AND NEW.booklist_id IS NOT DISTINCT FROM OLD.booklist_id THEN
        RETURN NULL;
      END IF;
      IF TG_OP<>'INSERT' THEN previous:=OLD.work_id; END IF;
      IF TG_OP<>'DELETE' THEN following:=NEW.work_id; END IF;
      FOR identifier IN SELECT DISTINCT value FROM unnest(ARRAY[previous,following]) AS ids(value) WHERE value IS NOT NULL ORDER BY value LOOP
        PERFORM pg_advisory_xact_lock(identifier);
        INSERT INTO work_statistics(work_id,collection_count) SELECT identifier,count(DISTINCT coalesce(b.fluxer_owner_id,b.owner_id::text)) FROM booklist_item i JOIN booklist b ON b.id=i.booklist_id WHERE i.work_id=identifier AND b.is_default
        ON CONFLICT(work_id) DO UPDATE SET collection_count=excluded.collection_count;
      END LOOP;
      RETURN NULL;
    END $$"""


def upgrade():
    """Replace functions atomically; retain all existing triggers and constraints."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(PROJECT_GUARDED)
    op.execute(COLLECTION_GUARDED)


def downgrade():
    """Restore the previous projections without changing stored data."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(PROJECT_PREVIOUS)
    op.execute(COLLECTION_PREVIOUS)
