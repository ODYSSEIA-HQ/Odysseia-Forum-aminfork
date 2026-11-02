"""作品关注领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from sqlalchemy import select
from shared.time_utils import utc_now
from models.thread import Thread
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkFollowService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def follow(self, work_id: int, command: WorkCommandQo):
        """关注只影响未来公告，不补发历史通知。"""
        principal = command.actor
        from models.thread_follow import ThreadFollow

        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal)
            row = (
                (
                    await session.execute(
                        select(ThreadFollow)
                        .where(
                            ThreadFollow.fluxer_user_id == principal["id"],
                            ThreadFollow.work_id == work_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if row is None and command.operation == "follow":
                main = (
                    (
                        await session.execute(
                            select(Thread).where(
                                Thread.work_id == work_id,
                                Thread.role == "main",
                                Thread.platform == "discord",
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                session.add(
                    ThreadFollow(
                        fluxer_user_id=principal["id"],
                        user_id=int(principal["discord_user_id"]),
                        work_id=work_id,
                        thread_id=main.thread_id if main else None,
                        followed_at=utc_now(),
                        active_flag=True,
                    )
                )
            elif row is not None:
                row.active_flag = command.operation == "follow"
                if row.active_flag:
                    row.followed_at = utc_now()
            await session.commit()
        return {"followed": command.operation == "follow"}

    async def follows(self, offset: int, limit: int, command: WorkCommandQo):
        """关注列表采用统一作品可见性。"""
        principal = command.actor
        from models.thread_follow import ThreadFollow

        async with self.sessions() as session:
            ids = select(ThreadFollow.work_id).where(
                ThreadFollow.fluxer_user_id == principal["id"],
                ThreadFollow.active_flag.is_(True),
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
