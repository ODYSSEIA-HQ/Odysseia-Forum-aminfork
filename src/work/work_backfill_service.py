"""可重入、小批次的作品关联回填。"""

from sqlalchemy import text


class WorkBackfillService:
    """PG触发器持续捕获新写入，回填不依赖会漏更新的时间游标。"""

    ASSOCIATIONS = {
        "booklist_item": ("thread_id", "external"),
        "thread_follow": ("thread_id", "external"),
        "notification": ("thread_id", "external"),
        "thread_update": ("thread_id", "external"),
        "tag_binding": ("target_id", "internal"),
        "tag_proposal": ("target_id", "internal"),
        "tag_proposal_block": ("target_id", "internal"),
    }

    def __init__(self, session):
        """一批一事务，由调用方控制提交。"""
        self.session = session

    async def threads(self, limit):
        """触发器在小批更新中原子建立主作品。"""
        rows = await self.session.execute(
            text(
                "WITH batch AS (SELECT id FROM thread WHERE work_id IS NULL ORDER BY id FOR UPDATE SKIP LOCKED LIMIT :limit) UPDATE thread t SET work_id=t.work_id FROM batch b WHERE t.id=b.id RETURNING t.id"
            ),
            {"limit": limit},
        )
        return len(rows.all())

    async def association(self, table, limit):
        """仅处理固定白名单，并在源引用未变化时更新。"""
        if table not in self.ASSOCIATIONS:
            raise ValueError("未知回填关联表")
        column, kind = self.ASSOCIATIONS[table]
        match = (
            f"r.thread_id=t.{column} AND r.platform='discord'"
            if kind == "external"
            else f"r.id=t.{column} AND t.target_type='thread'"
        )
        statement = f"WITH batch AS (SELECT t.ctid AS row_id,t.{column} AS source_id,r.work_id FROM {table} t JOIN thread r ON {match} WHERE t.work_id IS NULL AND r.work_id IS NOT NULL ORDER BY t.ctid LIMIT :limit FOR UPDATE OF t SKIP LOCKED) UPDATE {table} t SET work_id=b.work_id FROM batch b WHERE t.ctid=b.row_id AND t.{column} IS NOT DISTINCT FROM b.source_id RETURNING t.work_id"
        rows = await self.session.execute(text(statement), {"limit": limit})
        return len(rows.all())

    async def audit(self):
        """检查未映射来源和关联，不自动删除无法解释的历史记录。"""
        result = {
            "thread": (
                await self.session.execute(
                    text("SELECT count(*) FROM thread WHERE work_id IS NULL")
                )
            ).scalar_one()
        }
        for table, (column, kind) in self.ASSOCIATIONS.items():
            condition = (
                f"{column} IS NOT NULL"
                if kind == "external"
                else "target_type='thread'"
            )
            result[table] = (
                await self.session.execute(
                    text(
                        f"SELECT count(*) FROM {table} WHERE work_id IS NULL AND {condition}"
                    )
                )
            ).scalar_one()
        return result
