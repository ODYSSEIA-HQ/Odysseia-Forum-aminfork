"""作品持久展示计数仓储。"""

from sqlalchemy.dialects.postgresql import insert
from models.work_statistics import WorkStatistics


class WorkStatisticsRepository:
    """集合更新一页作品的展示计数，供既有 UCB1 排序使用。"""

    def __init__(self, session):
        """复用调用方会话，不单独提交事务。"""
        self.session = session

    async def record_impressions(self, work_ids):
        """每页去重后原子累加，不建立逐用户展示明细。"""
        ids = set(work_ids)
        if not ids:
            return
        statement = insert(WorkStatistics).values(
            [{"work_id": value, "display_count": 1} for value in ids]
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[WorkStatistics.work_id],
                set_={"display_count": WorkStatistics.display_count + 1},
            )
        )
