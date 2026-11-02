"""分批投影、条件回填与切换前审计。"""

import json
from sqlalchemy import text, inspect
from identity.reference_manifest import (
    REFERENCES,
    OWNERSHIP_REFERENCES,
    AUTHOR_LISTS,
    fluxer_column,
)


class MigrationService:
    """只处理 Fluxer 权威导出，不生成或猜测统一用户 ID。"""

    def __init__(self, engine):
        """复用显式提供的数据库引擎执行迁移批次。"""
        self.engine = engine

    def import_batch(self, records):
        """历史身份按 Discord 去重，用户资料按统一主体去重，旧快照不能复活登录。"""
        clean = []
        seen_discord = set()
        current_users = set()
        for row in records:
            user_id = str(row["user_id"])
            discord_id = str(row["discord_user_id"])
            version = row.get("version", 1)
            current = row.get("is_current", True)
            if (
                not user_id.isdecimal()
                or user_id == "0"
                or not discord_id.isdecimal()
                or discord_id == "0"
                or type(version) is not int
                or version < 1
                or type(current) is not bool
            ):
                raise ValueError("权威身份、版本或当前绑定标志无效")
            if discord_id in seen_discord or (current and user_id in current_users):
                raise ValueError("同批次 Discord 身份重复或同用户有多个当前绑定")
            seen_discord.add(discord_id)
            if current:
                current_users.add(user_id)
            clean.append(
                {
                    **row,
                    "user_id": user_id,
                    "discord_user_id": discord_id,
                    "version": version,
                    "is_current": current,
                }
            )
        if not clean:
            return 0
        payload = json.dumps(clean)
        with self.engine.begin() as conn:
            history = any(
                column["name"] == "is_current"
                for column in inspect(conn).get_columns("discord_identity")
            )
            if not history and len({row["user_id"] for row in clean}) != len(clean):
                raise ValueError("请先升级 fluxer_identity_history 再导入多历史身份")
            incoming = "jsonb_to_recordset(CAST(:payload AS jsonb)) AS x(discord_user_id text,user_id text,version bigint,is_current boolean,username text,display_name text,avatar_url text)"
            canonical = (
                "coalesce(canonical_forum_user(x.user_id),x.user_id)"
                if history
                else "x.user_id"
            )
            conflict = conn.execute(
                text(
                    f"SELECT d.discord_user_id FROM discord_identity d JOIN {incoming} ON d.discord_user_id=x.discord_user_id WHERE d.user_id<>{canonical} LIMIT 1"
                ),
                {"payload": payload},
            ).first()
            if conflict:
                raise ValueError("绑定冲突：需要显式管理合并，普通导入不能转移")
            # 重复用户只 upsert 一次，历史快照不得覆盖更高版本的当前资料。
            conn.execute(
                text(f"""INSERT INTO "user"(id,username,display_name,avatar_url)
              SELECT DISTINCT ON(x.user_id) x.user_id,x.username,x.display_name,x.avatar_url FROM {incoming}
              WHERE x.version >= coalesce((SELECT max(d.version) FROM discord_identity d WHERE d.user_id=x.user_id),0)
              ORDER BY x.user_id,x.is_current DESC,x.version DESC,x.discord_user_id
              ON CONFLICT(id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,avatar_url=excluded.avatar_url,updated_at=now()"""),
                {"payload": payload},
            )
            if history:
                # 只有更高版本的新当前绑定才能撤销旧当前绑定。
                conn.execute(
                    text(f"""UPDATE discord_identity d SET is_current=false FROM {incoming}
                  WHERE x.is_current AND d.user_id={canonical} AND d.discord_user_id<>x.discord_user_id
                  AND x.version>(SELECT coalesce(max(existing.version),0) FROM discord_identity existing WHERE existing.user_id=d.user_id)"""),
                    {"payload": payload},
                )
                conn.execute(
                    text(f"""INSERT INTO discord_identity(discord_user_id,user_id,version,is_current)
                  SELECT x.discord_user_id,{canonical},x.version,
                    x.is_current AND x.version >= coalesce((SELECT max(d.version) FROM discord_identity d WHERE d.user_id={canonical}),0)
                  FROM {incoming}
                  ON CONFLICT(discord_user_id) DO UPDATE SET version=greatest(discord_identity.version,excluded.version),is_current=excluded.is_current
                  WHERE excluded.version>discord_identity.version OR (excluded.version=discord_identity.version AND NOT excluded.is_current)"""),
                    {"payload": payload},
                )
            else:
                conn.execute(
                    text(f"""INSERT INTO discord_identity(discord_user_id,user_id,version) SELECT x.discord_user_id,x.user_id,x.version FROM {incoming}
                  ON CONFLICT(discord_user_id) DO UPDATE SET version=excluded.version WHERE excluded.version>discord_identity.version"""),
                    {"payload": payload},
                )
        return len(clean)

    def backfill_batch(self, table, column, limit):
        """锁定有限行并校验源快照，原系统并发改作者时不会被覆盖。"""
        if table not in REFERENCES or column not in REFERENCES[table]:
            raise ValueError("未知身份列")
        target = fluxer_column(column)
        # 历史操作者只补空值，显式管理合并后不重写其原始统一账户快照。
        owner_column = column in OWNERSHIP_REFERENCES.get(table, [])
        mismatch = (
            f"t.{target} IS DISTINCT FROM d.user_id"
            if owner_column
            else f"t.{target} IS NULL"
        )
        # NULL 与系统零保留原语义；无映射的行由审计阻止切换。
        statement = f"""WITH batch AS (
            SELECT t.ctid AS row_id,t.{column} AS source_id,d.user_id
            FROM {table} t JOIN discord_identity d ON d.discord_user_id=t.{column}::text
            WHERE t.{column}<>0 AND {mismatch}
            ORDER BY t.ctid LIMIT :limit FOR UPDATE OF t SKIP LOCKED)
            UPDATE {table} t SET {target}=b.user_id FROM batch b
            WHERE t.ctid=b.row_id AND t.{column} IS NOT DISTINCT FROM b.source_id"""
        with self.engine.begin() as conn:
            return conn.execute(text(statement), {"limit": limit}).rowcount

    def backfill_lists(self, table, column, limit):
        """只在全部作者映射齐全时原子替换列表，保留顺序和重复项。"""
        if column not in AUTHOR_LISTS.get(table, []):
            raise ValueError("未知作者列表")
        target = "fluxer_" + column
        statement = f"""WITH candidates AS (
            SELECT t.ctid AS row_id,t.{column}::jsonb AS source_ids,
                COALESCE((SELECT jsonb_agg(d.user_id ORDER BY a.ordinality)
                FROM jsonb_array_elements_text(t.{column}::jsonb) WITH ORDINALITY a(id,ordinality)
                JOIN discord_identity d ON d.discord_user_id=a.id),'[]'::jsonb) AS ids
            FROM {table} t WHERE t.{column} IS NOT NULL
                AND t.{target}::jsonb IS DISTINCT FROM COALESCE((SELECT jsonb_agg(d.user_id ORDER BY a.ordinality)
                FROM jsonb_array_elements_text(t.{column}::jsonb) WITH ORDINALITY a(id,ordinality)
                JOIN discord_identity d ON d.discord_user_id=a.id),'[]'::jsonb)
                AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(t.{column}::jsonb) a(id)
                LEFT JOIN discord_identity d ON d.discord_user_id=a.id WHERE d.user_id IS NULL)
            ORDER BY t.ctid LIMIT :limit FOR UPDATE OF t SKIP LOCKED),
            batch AS (SELECT * FROM candidates WHERE ids IS DISTINCT FROM
                (SELECT {target}::jsonb FROM {table} t WHERE t.ctid=candidates.row_id))
            UPDATE {table} t SET {target}=b.ids::json FROM batch b
            WHERE t.ctid=b.row_id AND t.{column}::jsonb IS NOT DISTINCT FROM b.source_ids"""
        with self.engine.begin() as conn:
            return conn.execute(text(statement), {"limit": limit}).rowcount

    def backfill_vote_keys(self, limit):
        """历史投票代理键分批填充，不在 DDL 事务中全表改写。"""
        with self.engine.begin() as conn:
            return conn.execute(
                text("""WITH batch AS (SELECT ctid FROM tag_vote WHERE id IS NULL
                LIMIT :limit FOR UPDATE SKIP LOCKED) UPDATE tag_vote t SET id=nextval('fluxer_tag_vote_id_seq')
                FROM batch WHERE t.ctid=batch.ctid AND t.id IS NULL"""),
                {"limit": limit},
            ).rowcount

    def audit(self):
        """统计全部缺失、失配和孤立身份，任何非零计数都阻止切换。"""
        result = {}
        with self.engine.connect() as conn:
            history = any(
                column["name"] == "merged_into_id"
                for column in inspect(conn).get_columns("user")
            )
            result["tag_vote.id"] = conn.execute(
                text("SELECT count(*) FROM tag_vote WHERE id IS NULL")
            ).scalar_one()
            for table, columns in REFERENCES.items():
                for column in columns:
                    target = fluxer_column(column)
                    owner_column = column in OWNERSHIP_REFERENCES.get(table, [])
                    mismatch = f"t.{target} IS DISTINCT FROM d.user_id"
                    if history and not owner_column:
                        mismatch = f"canonical_forum_user(t.{target}) IS DISTINCT FROM canonical_forum_user(d.user_id)"
                    result[f"{table}.{column}"] = conn.execute(
                        text(f"""SELECT count(*) FROM {table} t
                        LEFT JOIN discord_identity d ON d.discord_user_id=t.{column}::text
                        LEFT JOIN "user" u ON u.id=t.{target}
                        WHERE (t.{column}<>0 AND t.{column} IS NOT NULL AND
                        (d.user_id IS NULL OR {mismatch}))
                        OR (t.{target} IS NOT NULL AND (u.id IS NULL OR t.{column}=0))""")
                    ).scalar_one()
            for table, columns in AUTHOR_LISTS.items():
                for column in columns:
                    owner_column = column in OWNERSHIP_REFERENCES.get(table, [])
                    mismatch = f"t.{target} IS DISTINCT FROM d.user_id"
                    if history and not owner_column:
                        mismatch = f"canonical_forum_user(t.{target}) IS DISTINCT FROM canonical_forum_user(d.user_id)"
                    result[f"{table}.{column}"] = conn.execute(
                        text(f"""SELECT count(*) FROM {table} t
                        WHERE t.{column} IS NOT NULL AND (t.fluxer_{column}::jsonb IS DISTINCT FROM
                        COALESCE((SELECT jsonb_agg(d.user_id ORDER BY a.ordinality)
                        FROM jsonb_array_elements_text(t.{column}::jsonb) WITH ORDINALITY a(id,ordinality)
                        JOIN discord_identity d ON d.discord_user_id=a.id),'[]'::jsonb) OR EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(t.{column}::jsonb) a(id)
                        LEFT JOIN discord_identity d ON d.discord_user_id=a.id WHERE d.user_id IS NULL))""")
                    ).scalar_one()
        return result
