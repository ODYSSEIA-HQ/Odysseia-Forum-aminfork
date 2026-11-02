"""作品书单领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from sqlalchemy import select
from models.booklist import Booklist
from models.booklist_item import BooklistItem
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkBooklistService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def booklists(self, command: WorkCommandQo):
        """返回当前用户的普通及默认书单。"""
        principal = command.actor
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(Booklist)
                        .where(
                            Booklist.fluxer_owner_id == principal["id"],
                            Booklist.merged_into_id.is_(None),
                        )
                        .order_by(Booklist.id)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "description": row.description,
                        "is_public": row.is_public,
                        "is_default": row.is_default,
                        "item_count": row.item_count,
                    }
                    for row in rows
                ]
            }

    async def create_booklist(self, command: WorkCommandQo):
        """创建作品书单，所有者来自认证主体。"""
        principal = command.actor
        body = dict(command.values)
        if (
            not isinstance(body.get("title"), str)
            or not body["title"].strip()
            or set(body) - {"title", "description", "is_public"}
        ):
            raise HTTPException(422, "无效书单字段")
        async with self.sessions() as session:
            row = Booklist(
                title=body["title"],
                description=body.get("description"),
                is_public=body.get("is_public", True),
                owner_id=int(principal["discord_user_id"]),
                fluxer_owner_id=principal["id"],
            )
            session.add(row)
            await session.flush()
            result = {
                "id": str(row.id),
                "title": row.title,
                "description": row.description,
                "is_public": row.is_public,
                "is_default": False,
                "item_count": 0,
            }
            await session.commit()
            return result

    async def booklist_detail(
        self, booklist_id: int, offset: int, limit: int, command: WorkCommandQo
    ):
        """公开书单仍按访问者偏好过滤作品。"""
        principal = command.actor
        async with self.sessions() as session:
            from core.booklist_repository import BooklistRepository

            row = await BooklistRepository(session).get_booklist(booklist_id)
            if row is None or (
                not row.is_public and row.fluxer_owner_id != principal["id"]
            ):
                raise HTTPException(404, "书单不存在")
            ids = select(BooklistItem.work_id).where(BooklistItem.booklist_id == row.id)
            rows, total = await WorkRepository(session).page(
                principal, offset, limit, work_ids=ids
            )
            return {
                "id": str(row.id),
                "title": row.title,
                "description": row.description,
                "is_public": row.is_public,
                "is_default": row.is_default,
                "items": await WorkService(session).project(rows),
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def edit_booklist(self, booklist_id: int, command: WorkCommandQo):
        """更新书单元数据，禁止重定向所有权。"""
        principal = command.actor
        body = dict(command.values)
        if set(body) - {"title", "description", "is_public"}:
            raise HTTPException(422, "无效书单字段")
        async with self.sessions() as session:
            from core.booklist_repository import BooklistRepository

            row = await BooklistRepository(session).get_booklist(booklist_id)
            if row is None or row.fluxer_owner_id != principal["id"]:
                raise HTTPException(404, "书单不存在")
            for key, value in body.items():
                setattr(row, key, value)
            await session.flush()
            result = {
                "id": str(row.id),
                "title": row.title,
                "description": row.description,
                "is_public": row.is_public,
                "is_default": row.is_default,
                "item_count": row.item_count,
            }
            await session.commit()
            return result

    async def booklist_item(
        self, booklist_id: int, work_id: int, command: WorkCommandQo
    ):
        """书单条目以作品为单位去重。"""
        principal = command.actor
        async with self.sessions() as session:
            service = WorkService(session)
            work = await service.visible(work_id, principal)
            result = await service.collect(
                work, principal, command.operation == "add", booklist_id
            )
            await session.commit()
            return result
