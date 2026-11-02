"""对缺失权威身份的持续写入执行有界追增。"""

from sqlalchemy import text
from identity.reference_manifest import REFERENCES, AUTHOR_LISTS, fluxer_column


class IdentityProjectionService:
    """PG 列桶保留待办，Redis 或进程重启不会丢失旧写入。"""

    def __init__(self, session):
        """使用调用方事务，补全空投影不改历史操作者。"""
        self.session = session

    async def once(self, limit=500):
        """每个列桶最多补全有限行，映射尚未到达时保留桶。"""
        buckets = (
            await self.session.execute(
                text(
                    "SELECT table_name,column_name FROM identity_pending_projection ORDER BY table_name,column_name FOR UPDATE SKIP LOCKED LIMIT 32"
                )
            )
        ).all()
        changed = 0
        for table, column in buckets:
            if column in AUTHOR_LISTS.get(table, []):
                target = "fluxer_" + column
                pending = f"t.{column} IS NOT NULL AND t.{target} IS NULL"
                statement = f"""WITH batch AS (
                  SELECT t.ctid AS row_id, COALESCE((SELECT jsonb_agg(canonical_forum_user(d.user_id) ORDER BY a.ord)
                  FROM jsonb_array_elements_text(t.{column}::jsonb) WITH ORDINALITY a(id,ord)
                  JOIN discord_identity d ON d.discord_user_id=a.id),'[]'::jsonb) AS ids
                  FROM {table} t WHERE {pending} AND NOT EXISTS (
                  SELECT 1 FROM jsonb_array_elements_text(t.{column}::jsonb) a(id)
                  LEFT JOIN discord_identity d ON d.discord_user_id=a.id WHERE d.user_id IS NULL)
                  LIMIT :limit FOR UPDATE OF t SKIP LOCKED)
                  UPDATE {table} t SET {target}=batch.ids::json FROM batch WHERE t.ctid=batch.row_id"""
            elif column in REFERENCES.get(table, []):
                target = fluxer_column(column)
                pending = (
                    f"t.{column} IS NOT NULL AND t.{column}<>0 AND t.{target} IS NULL"
                )
                statement = f"""WITH batch AS (
                  SELECT t.ctid AS row_id,canonical_forum_user(d.user_id) AS user_id
                  FROM {table} t JOIN discord_identity d ON d.discord_user_id=t.{column}::text
                  WHERE {pending} LIMIT :limit FOR UPDATE OF t SKIP LOCKED)
                  UPDATE {table} t SET {target}=batch.user_id FROM batch WHERE t.ctid=batch.row_id"""
            else:
                raise ValueError("未知身份投影列桶")
            changed += (
                await self.session.execute(text(statement), {"limit": limit})
            ).rowcount
            # 未知身份仍保留；桶行锁保证并发捕获与清桶不会丢通知。
            remaining = (
                await self.session.execute(
                    text(f"SELECT EXISTS(SELECT 1 FROM {table} t WHERE {pending})")
                )
            ).scalar_one()
            if not remaining:
                await self.session.execute(
                    text(
                        "DELETE FROM identity_pending_projection WHERE table_name=:table AND column_name=:column"
                    ),
                    {"table": table, "column": column},
                )
        return changed
