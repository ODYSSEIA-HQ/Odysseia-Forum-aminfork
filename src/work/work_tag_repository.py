"""作品标签候选单表仓储。"""

from sqlalchemy import select
from models.tag import Tag


class WorkTagRepository:
    """读取有效标准概念，不产生新的绑定或审核事实。"""

    def __init__(self, session):
        """复用调用方会话。"""
        self.session = session

    async def candidates(self, query, can_view_abyss):
        """集合查询至多一百项，应用名称和深渊发现过滤。"""
        statement = select(Tag).where(Tag.deleted_at.is_(None))
        if query:
            statement = statement.where(Tag.name.ilike("%" + query + "%"))
        if not can_view_abyss:
            statement = statement.where(Tag.is_abyss.is_(False))
        return (
            (
                await self.session.execute(
                    statement.order_by(Tag.name, Tag.id).limit(100)
                )
            )
            .scalars()
            .all()
        )
