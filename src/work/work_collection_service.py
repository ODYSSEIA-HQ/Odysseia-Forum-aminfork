"""默认收藏领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from sqlalchemy import select
from models.booklist import Booklist
from models.booklist_item import BooklistItem
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkCollectionService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def collection(self, work_id: int, command: WorkCommandQo):
        """用默认书单承载收藏，不新建作品收藏表。"""
        principal = command.actor
        async with self.sessions() as session:
            service = WorkService(session)
            work = await service.visible(work_id, principal)
            result = await service.collect(
                work, principal, command.operation == "collect"
            )
            await session.commit()
            return result

    async def my_collection(self, offset: int, limit: int, command: WorkCommandQo):
        """收藏分页复用作品可见性和总数规则。"""
        principal = command.actor
        async with self.sessions() as session:
            ids = (
                select(BooklistItem.work_id)
                .join(Booklist, Booklist.id == BooklistItem.booklist_id)
                .where(
                    Booklist.fluxer_owner_id == principal["id"],
                    Booklist.is_default.is_(True),
                )
            )
            rows, total = await WorkRepository(session).page(
                principal, offset, limit, work_ids=ids
            )
            return {
                "items": await WorkService(session).project(rows),
                "total": total,
                "offset": offset,
                "limit": limit,
            }
