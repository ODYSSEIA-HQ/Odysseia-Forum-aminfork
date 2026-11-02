"""增量作品层：旧写入持续投影，V1/V2 可以同时运行。"""

from alembic import op
import sqlalchemy as sa

revision = "work_layer_prepare"
down_revision = "fluxer_identity_runtime"
branch_labels = None
depends_on = None


def upgrade():
    """新增作品并用 PG 触发器捕获旧进程的持续写入。"""
    op.execute("SET LOCAL lock_timeout = '5s'")
    for index in sa.inspect(op.get_bind()).get_indexes("author"):
        if index.get("unique") and index["column_names"] == ["fluxer_user_id"]:
            op.drop_index(index["name"], table_name="author")
    op.execute('ALTER TABLE "user" ADD COLUMN show_nsfw BOOLEAN NOT NULL DEFAULT false')
    op.execute("""CREATE TABLE work (
      id BIGSERIAL PRIMARY KEY, classification_revision BIGINT NOT NULL DEFAULT 0,source_visible BOOLEAN NOT NULL DEFAULT true,title VARCHAR NOT NULL, description VARCHAR, search_vector TSVECTOR, last_active_at TIMESTAMP,
      images JSON NOT NULL DEFAULT '[]', metadata_overrides JSON NOT NULL DEFAULT '[]', author_id VARCHAR,
      source_platform VARCHAR NOT NULL DEFAULT 'discord', source_guild_id VARCHAR NOT NULL DEFAULT '',
      source_channel_id VARCHAR NOT NULL DEFAULT '', source_thread_id VARCHAR NOT NULL DEFAULT '', main_ref VARCHAR NOT NULL DEFAULT '',
      is_nsfw BOOLEAN NOT NULL DEFAULT false, is_abyss BOOLEAN NOT NULL DEFAULT false,
      created_at TIMESTAMP NOT NULL DEFAULT now(), deleted_at TIMESTAMP, deletion_reason VARCHAR
    )""")
    op.execute("""CREATE TABLE work_statistics (work_id BIGINT PRIMARY KEY,
      display_count BIGINT NOT NULL DEFAULT 0, view_count BIGINT NOT NULL DEFAULT 0, collection_count BIGINT NOT NULL DEFAULT 0,
      reaction_count BIGINT NOT NULL DEFAULT 0, reply_count BIGINT NOT NULL DEFAULT 0,
      CONSTRAINT ck_work_statistics_nonnegative CHECK(view_count>=0 AND collection_count>=0 AND reaction_count>=0 AND reply_count>=0))""")
    op.execute("""CREATE TABLE work_reaction (work_id BIGINT NOT NULL, emoji VARCHAR NOT NULL, emoji_data JSON NOT NULL DEFAULT '{}', count INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(work_id,emoji), CONSTRAINT ck_work_reaction_count CHECK(count>=0))""")
    op.execute("""CREATE TABLE work_task (id BIGSERIAL PRIMARY KEY, work_id BIGINT NOT NULL,
      kind VARCHAR NOT NULL, dedupe_key VARCHAR NOT NULL UNIQUE, payload JSON NOT NULL DEFAULT '{}',
      status VARCHAR NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      available_at TIMESTAMP NOT NULL DEFAULT now(), dispatched_at TIMESTAMP, lease_until TIMESTAMP, error VARCHAR)""")
    op.execute(
        "ALTER TABLE thread_follow ADD COLUMN work_id BIGINT, ALTER COLUMN thread_id DROP NOT NULL"
    )
    op.execute("ALTER TABLE notification ADD COLUMN work_id BIGINT")
    op.execute("ALTER TABLE notification DROP CONSTRAINT ck_notification_event_type")
    op.execute(
        "ALTER TABLE notification ADD CONSTRAINT ck_notification_event_type CHECK(event_type IN ('thread_update','author_new_thread','tag_review','work_announcement'))"
    )
    op.create_unique_constraint(
        "uq_thread_follow_user_work", "thread_follow", ["fluxer_user_id", "work_id"]
    )
    op.execute(
        "CREATE TABLE work_source_state (platform VARCHAR NOT NULL,thread_id VARCHAR NOT NULL,version BIGINT NOT NULL,deleted BOOLEAN NOT NULL DEFAULT false,PRIMARY KEY(platform,thread_id))"
    )
    op.execute(
        "CREATE TABLE work_event (event_id VARCHAR PRIMARY KEY,payload JSON NOT NULL,received_at TIMESTAMP NOT NULL DEFAULT now())"
    )
    op.execute("CREATE INDEX ix_work_task_due ON work_task(status,available_at)")
    op.execute("""CREATE TABLE work_announcement (id BIGSERIAL PRIMARY KEY, work_id BIGINT NOT NULL,
      author_id VARCHAR,legacy_update_id BIGINT UNIQUE,title VARCHAR, body VARCHAR NOT NULL, images JSON NOT NULL DEFAULT '[]',
      created_at TIMESTAMP NOT NULL DEFAULT now(), withdrawn_at TIMESTAMP)""")
    op.execute(
        "ALTER TABLE thread ADD COLUMN deletion_reason VARCHAR, ADD COLUMN native_archived BOOLEAN NOT NULL DEFAULT false, ADD COLUMN initializing BOOLEAN NOT NULL DEFAULT false, ADD COLUMN source_author_id VARCHAR, ADD COLUMN platform_locked BOOLEAN NOT NULL DEFAULT false, ADD COLUMN source_version BIGINT NOT NULL DEFAULT 0, ADD COLUMN reaction_emojis JSON NOT NULL DEFAULT '{}', ADD COLUMN reactions JSON NOT NULL DEFAULT '{}', ADD COLUMN platform VARCHAR NOT NULL DEFAULT 'discord', ADD COLUMN work_id BIGINT, ADD COLUMN role VARCHAR NOT NULL DEFAULT 'main', ADD COLUMN body VARCHAR NOT NULL DEFAULT '', ADD COLUMN deleted_at TIMESTAMP, ADD COLUMN lock_owner_ids JSON NOT NULL DEFAULT '[]', ADD COLUMN archive_owner_ids JSON NOT NULL DEFAULT '[]', ADD COLUMN archived BOOLEAN NOT NULL DEFAULT false"
    )
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("thread"):
        if constraint["column_names"] == ["thread_id"]:
            op.drop_constraint(constraint["name"], "thread", type_="unique")
    for index in inspector.get_indexes("thread"):
        if (
            index.get("unique")
            and index["column_names"] == ["thread_id"]
            and not index.get("duplicates_constraint")
        ):
            op.drop_index(index["name"], table_name="thread")
    op.create_index(
        "uq_thread_platform_external", "thread", ["platform", "thread_id"], unique=True
    )
    op.create_index(
        "uq_work_main_thread",
        "thread",
        ["work_id"],
        unique=True,
        postgresql_where=sa.text("work_id IS NOT NULL AND role='main'"),
    )
    op.execute(
        "ALTER TABLE thread ADD CONSTRAINT ck_thread_role CHECK(role IN ('main','child'))"
    )
    op.add_column(
        "booklist", sa.Column("merged_into_id", sa.BigInteger(), nullable=True)
    )
    op.add_column("booklist_item", sa.Column("work_id", sa.BigInteger(), nullable=True))
    op.alter_column("booklist_item", "thread_id", nullable=True)
    op.create_unique_constraint(
        "uq_booklist_work", "booklist_item", ["booklist_id", "work_id"]
    )
    op.execute("ALTER TABLE tag_binding DROP CONSTRAINT ck_tag_binding")
    op.execute(
        "ALTER TABLE tag_binding ADD CONSTRAINT ck_tag_binding CHECK(target_type IN ('thread','booklist','work') AND binding_source IN ('discord_sync','local') AND (binding_source<>'discord_sync' OR target_type='thread') AND (binding_source<>'discord_sync' OR discord_source_id IS NOT NULL) AND (binding_source<>'local' OR discord_source_id IS NULL) AND upvotes>=0 AND downvotes>=0)"
    )
    op.create_index(
        "ix_work_search_vector", "work", ["search_vector"], postgresql_using="gin"
    )
    op.execute(
        "CREATE TABLE work_user_mute (id BIGSERIAL PRIMARY KEY,work_id BIGINT NOT NULL,user_id VARCHAR NOT NULL,actor_id VARCHAR NOT NULL,reason VARCHAR NOT NULL,created_at TIMESTAMP NOT NULL DEFAULT now(),expires_at TIMESTAMP,revoked_at TIMESTAMP)"
    )
    op.create_index(
        "uq_active_work_mute_source",
        "work_user_mute",
        ["work_id", "user_id", "actor_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.execute("ALTER TABLE tag DROP CONSTRAINT ck_tag_source_category")
    op.execute(
        "ALTER TABLE tag ADD CONSTRAINT ck_tag_source_category CHECK(source IN ('custom','discord','fluxer') AND (category BETWEEN 1 AND 7 OR category IS NULL) AND (source<>'custom' OR category IS NOT NULL OR originated_from_discord))"
    )
    op.create_index(
        "uq_live_fluxer_tag_name",
        "tag",
        ["name"],
        unique=True,
        postgresql_where=sa.text("source='fluxer' AND deleted_at IS NULL"),
    )
    op.execute(
        "CREATE TABLE work_native_tag_source (id BIGSERIAL PRIMARY KEY,platform VARCHAR NOT NULL,guild_id VARCHAR NOT NULL,channel_id VARCHAR NOT NULL,external_id VARCHAR NOT NULL,tag_id BIGINT NOT NULL,name VARCHAR NOT NULL,emoji JSON NOT NULL DEFAULT '{}',CONSTRAINT uq_work_native_tag_identity UNIQUE(platform,guild_id,channel_id,external_id))"
    )
    op.execute(
        "CREATE TABLE work_native_tag_binding (work_id BIGINT NOT NULL,source_id BIGINT NOT NULL,PRIMARY KEY(work_id,source_id))"
    )
    op.add_column("thread_update", sa.Column("work_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "notification",
        sa.Column(
            "event_source_platform",
            sa.String(),
            nullable=False,
            server_default="discord",
        ),
    )
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("notification"):
        columns = constraint["column_names"]
        if set(columns) == {"user_id", "event_type", "event_source_id"}:
            op.drop_constraint(constraint["name"], "notification", type_="unique")
            op.create_unique_constraint(
                constraint["name"], "notification", columns + ["event_source_platform"]
            )
    for index in inspector.get_indexes("notification"):
        columns = index["column_names"]
        if (
            index.get("unique")
            and set(columns) == {"fluxer_user_id", "event_type", "event_source_id"}
            and not index.get("duplicates_constraint")
        ):
            op.drop_index(index["name"], table_name="notification")
            op.create_index(
                index["name"] + "_platform",
                "notification",
                columns + ["event_source_platform"],
                unique=True,
            )
    for table in ("tag_binding", "tag_proposal", "tag_proposal_block"):
        op.add_column(table, sa.Column("work_id", sa.BigInteger(), nullable=True))
    op.create_index(
        "uq_active_local_work_tag",
        "tag_binding",
        ["work_id", "tag_id"],
        unique=True,
        postgresql_where=sa.text(
            "work_id IS NOT NULL AND binding_source='local' AND ended_at IS NULL"
        ),
    )
    op.create_index(
        "uq_pending_work_tag_proposal",
        "tag_proposal",
        ["work_id", "tag_id"],
        unique=True,
        postgresql_where=sa.text("work_id IS NOT NULL AND status='pending'"),
    )
    # 旧进程仍以 Discord 字段写入；同步创建作品，任务保留用于可靠追增与重算。
    op.execute("""CREATE FUNCTION project_thread_work() RETURNS trigger LANGUAGE plpgsql AS $$
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
    END $$""")
    op.execute(
        "CREATE TRIGGER project_thread_work BEFORE INSERT OR UPDATE ON thread FOR EACH ROW EXECUTE FUNCTION project_thread_work()"
    )
    op.execute("""CREATE FUNCTION capture_booklist_work() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.work_id IS NULL AND NEW.thread_id IS NOT NULL THEN
        SELECT work_id INTO NEW.work_id FROM thread WHERE platform='discord' AND thread_id=NEW.thread_id;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER capture_booklist_work BEFORE INSERT OR UPDATE ON booklist_item FOR EACH ROW EXECUTE FUNCTION capture_booklist_work()"
    )
    for table in ("thread_follow", "notification", "thread_update"):
        op.execute(
            f"CREATE TRIGGER capture_{table}_work BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION capture_booklist_work()"
        )
    op.execute("""CREATE FUNCTION lock_default_booklist() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.is_default THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('default-booklist:'||coalesce(NEW.fluxer_owner_id,NEW.owner_id::text),0));
        IF EXISTS(SELECT 1 FROM booklist WHERE is_default AND id IS DISTINCT FROM NEW.id AND coalesce(fluxer_owner_id,owner_id::text)=coalesce(NEW.fluxer_owner_id,NEW.owner_id::text)) THEN
          RAISE EXCEPTION 'default booklist already exists' USING ERRCODE='23505';
        END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER z_lock_default_booklist BEFORE INSERT OR UPDATE ON booklist FOR EACH ROW EXECUTE FUNCTION lock_default_booklist()"
    )
    op.execute("""CREATE FUNCTION refresh_work_collections() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE identifier BIGINT;
    BEGIN
      identifier:=CASE WHEN TG_OP='DELETE' THEN OLD.work_id ELSE NEW.work_id END;
      IF identifier IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(identifier);
        INSERT INTO work_statistics(work_id,collection_count) SELECT identifier,count(DISTINCT coalesce(b.fluxer_owner_id,b.owner_id::text)) FROM booklist_item i JOIN booklist b ON b.id=i.booklist_id WHERE i.work_id=identifier AND b.is_default
        ON CONFLICT(work_id) DO UPDATE SET collection_count=excluded.collection_count;
      END IF;
      RETURN NULL;
    END $$""")
    op.execute(
        "CREATE TRIGGER refresh_work_collections AFTER INSERT OR UPDATE OR DELETE ON booklist_item FOR EACH ROW EXECUTE FUNCTION refresh_work_collections()"
    )
    op.execute("""CREATE FUNCTION project_legacy_announcement() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.work_id IS NOT NULL THEN
        INSERT INTO work_announcement(work_id,author_id,title,body,images,created_at,legacy_update_id)
        VALUES(NEW.work_id,NEW.fluxer_publisher_id,NEW.version,NEW.description,'[]'::json,NEW.published_at,NEW.id)
        ON CONFLICT(legacy_update_id) DO UPDATE SET body=excluded.body,title=excluded.title;
      END IF;
      RETURN NULL;
    END $$""")
    op.execute(
        "CREATE TRIGGER project_legacy_announcement AFTER INSERT OR UPDATE ON thread_update FOR EACH ROW EXECUTE FUNCTION project_legacy_announcement()"
    )
    op.execute("""CREATE OR REPLACE FUNCTION refresh_work_collections() RETURNS trigger LANGUAGE plpgsql AS $$
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
    END $$""")
    op.execute("""CREATE FUNCTION refresh_booklist_work_statistics() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE identifier BIGINT; book BIGINT;
    BEGIN
      book:=CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
      FOR identifier IN SELECT DISTINCT work_id FROM booklist_item WHERE booklist_id=book AND work_id IS NOT NULL ORDER BY work_id LOOP
        PERFORM pg_advisory_xact_lock(identifier);
        INSERT INTO work_statistics(work_id,collection_count) SELECT identifier,count(DISTINCT coalesce(b.fluxer_owner_id,b.owner_id::text)) FROM booklist_item i JOIN booklist b ON b.id=i.booklist_id WHERE i.work_id=identifier AND b.is_default
        ON CONFLICT(work_id) DO UPDATE SET collection_count=excluded.collection_count;
      END LOOP;
      RETURN NULL;
    END $$""")
    op.execute(
        "CREATE TRIGGER refresh_booklist_work_statistics AFTER UPDATE OF is_default,fluxer_owner_id,owner_id OR DELETE ON booklist FOR EACH ROW EXECUTE FUNCTION refresh_booklist_work_statistics()"
    )
    op.execute("""CREATE FUNCTION capture_tag_work_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.target_type='work' THEN NEW.work_id:=NEW.target_id;
      ELSIF NEW.target_type='thread' THEN SELECT work_id INTO NEW.work_id FROM thread WHERE id=NEW.target_id;
      ELSE NEW.work_id:=NULL; END IF;
      RETURN NEW;
    END $$""")
    for table in ("tag_binding", "tag_proposal", "tag_proposal_block"):
        op.execute(
            f"CREATE TRIGGER capture_tag_work_identity BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION capture_tag_work_identity()"
        )
    op.execute("""CREATE FUNCTION project_discord_work_native_tag() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE origin RECORD; reference RECORD; source_identifier BIGINT;
    BEGIN
      IF NEW.target_type<>'thread' OR NEW.binding_source<>'discord_sync' OR NEW.work_id IS NULL THEN RETURN NULL; END IF;
      SELECT * INTO reference FROM thread WHERE id=NEW.target_id AND role='main';
      IF NOT FOUND THEN RETURN NULL; END IF;
      SELECT * INTO origin FROM discord_tag_source WHERE id=NEW.discord_source_id;
      IF NOT FOUND THEN RETURN NULL; END IF;
      INSERT INTO work_native_tag_source(platform,guild_id,channel_id,external_id,tag_id,name,emoji)
      VALUES('discord',reference.guild_id::text,reference.channel_id::text,origin.discord_tag_id::text,origin.tag_id,origin.name,'{}'::json)
      ON CONFLICT(platform,guild_id,channel_id,external_id) DO UPDATE SET tag_id=excluded.tag_id,name=excluded.name RETURNING id INTO source_identifier;
      IF NEW.ended_at IS NULL THEN
        INSERT INTO work_native_tag_binding(work_id,source_id) VALUES(NEW.work_id,source_identifier) ON CONFLICT DO NOTHING;
      ELSE
        DELETE FROM work_native_tag_binding WHERE work_id=NEW.work_id AND source_id=source_identifier;
      END IF;
      RETURN NULL;
    END $$""")
    op.execute(
        "CREATE TRIGGER project_discord_work_native_tag AFTER INSERT OR UPDATE ON tag_binding FOR EACH ROW EXECUTE FUNCTION project_discord_work_native_tag()"
    )
    op.execute("""CREATE FUNCTION protect_work_thread_delete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.work_id IS NOT NULL THEN
        RAISE EXCEPTION 'Work-linked Thread physical DELETE is disabled; use the compatible authoritative soft-delete handler' USING ERRCODE='55000';
      END IF;
      RETURN OLD;
    END $$""")
    op.execute(
        "CREATE TRIGGER protect_work_thread_delete BEFORE DELETE ON thread FOR EACH ROW EXECUTE FUNCTION protect_work_thread_delete()"
    )
    # 捕获缺映射写入所在的列桶，权威映射晚到后仍可有界追增。
    from identity.reference_manifest import REFERENCES, AUTHOR_LISTS, fluxer_column

    op.execute(
        "CREATE TABLE identity_pending_projection (table_name VARCHAR NOT NULL, column_name VARCHAR NOT NULL, PRIMARY KEY(table_name,column_name))"
    )
    for table, columns in REFERENCES.items():
        capture = []
        for column in columns:
            target = fluxer_column(column)
            op.execute(
                f"CREATE INDEX pending_identity_{table}_{column} ON {table} ({column}) WHERE {target} IS NULL AND {column} IS NOT NULL AND {column}<>0"
            )
            capture.append(
                f"IF NEW.{column} IS NOT NULL AND NEW.{column}<>0 AND NEW.{target} IS NULL THEN INSERT INTO identity_pending_projection VALUES ('{table}','{column}') ON CONFLICT DO NOTHING; END IF;"
            )
            op.execute(
                f"INSERT INTO identity_pending_projection VALUES ('{table}','{column}') ON CONFLICT DO NOTHING"
            )
        for column in AUTHOR_LISTS.get(table, []):
            capture.append(
                f"IF NEW.{column} IS NOT NULL AND NEW.fluxer_{column} IS NULL THEN INSERT INTO identity_pending_projection VALUES ('{table}','{column}') ON CONFLICT DO NOTHING; END IF;"
            )
            op.execute(
                f"INSERT INTO identity_pending_projection VALUES ('{table}','{column}') ON CONFLICT DO NOTHING"
            )
        op.execute(
            f"CREATE FUNCTION capture_pending_identity_{table}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {' '.join(capture)} RETURN NULL; END $$"
        )
        op.execute(
            f"CREATE TRIGGER capture_pending_identity_{table} AFTER INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION capture_pending_identity_{table}()"
        )

    # 不在 DDL 事务中全表回填，独立 CLI 分批推进并保留重入能力。


def downgrade():
    """已有作品与原生身份数据时只能前向修复。"""
    raise RuntimeError("作品层迁移不可自动回退")
