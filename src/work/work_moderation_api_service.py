"""作品管理领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from sqlalchemy import select
from shared.time_utils import utc_now
from models.work import Work
from models.user import User
from work.work_service import WorkService


class WorkModerationApiService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def restore_work(self, work_id: int, command: WorkCommandQo):
        """管理恢复普通隐藏作品，权威源主帖删除禁止恢复。"""
        principal = command.actor
        from work.work_moderation_service import WorkModerationService

        if not principal["is_admin"]:
            raise HTTPException(403, "需要管理组权限")
        async with self.sessions() as session:
            work = await session.get(Work, work_id, with_for_update=True)
            if work is None:
                raise HTTPException(404, "作品不存在")
            await WorkModerationService(session).soft_delete(work, principal, False)
            await session.commit()
        return {"restored": True}

    async def list_mutes(self, work_id: int, command: WorkCommandQo):
        """作品作者和管理可查看各独立处罚来源及到期时间。"""
        principal = command.actor
        from models.work_user_mute import WorkUserMute

        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal, owner=True)
            rows = (
                (
                    await session.execute(
                        select(WorkUserMute)
                        .where(WorkUserMute.work_id == work_id)
                        .order_by(WorkUserMute.id.desc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    {
                        **row.model_dump(),
                        "id": str(row.id),
                        "work_id": str(row.work_id),
                        "own_source": row.actor_id == principal["id"],
                    }
                    for row in rows
                ]
            }

    async def mute_user(self, work_id: int, command: WorkCommandQo):
        """作者或管理建立作品禁言并异步覆盖全部原生帖子。"""
        principal = command.actor
        body = dict(command.values)
        from datetime import datetime, timezone
        from work.work_moderation_service import WorkModerationService

        deadline = None
        if body.get("expires_at"):
            try:
                parsed = datetime.fromisoformat(
                    body["expires_at"].replace("Z", "+00:00")
                )
                deadline = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, "到期时间必须是包含时区的 ISO 时间") from exc
            if parsed.tzinfo is None or deadline <= utc_now():
                raise HTTPException(422, "到期时间必须在未来并包含时区")
        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, principal, owner=True)
            target = await session.get(User, body.get("user_id", ""))
            if target is None:
                raise HTTPException(404, "目标用户不存在")
            result = await WorkModerationService(session).mute(
                work, principal, target.id, body.get("reason"), deadline
            )
            await session.commit()
            return result

    async def unmute_user(self, work_id: int, mute_id: int, command: WorkCommandQo):
        """只释放当前操作者自己的作品处罚来源。"""
        principal = command.actor
        from work.work_moderation_service import WorkModerationService

        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, principal, owner=True)
            result = await WorkModerationService(session).mute(
                work, principal, None, None, restriction_id=mute_id
            )
            await session.commit()
            return result

    async def moderation_log(
        self, work_id: int, offset: int, limit: int, command: WorkCommandQo
    ):
        """作者或管理查询作品操作记录，保留实际操作者与原因。"""
        principal = command.actor
        from models.operation_log import OperationLog
        from sqlalchemy import func

        async with self.sessions() as session:
            work = await session.get(Work, work_id)
            if work is None or (
                work.author_id != principal["id"] and (not principal["is_admin"])
            ):
                raise HTTPException(404, "作品不存在")
            filters = [
                OperationLog.target_type == "work",
                OperationLog.target_id == work_id,
            ]
            total = (
                await session.execute(
                    select(func.count()).select_from(OperationLog).where(*filters)
                )
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(OperationLog)
                        .where(*filters)
                        .order_by(OperationLog.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    {
                        "id": str(row.id),
                        "type": row.type,
                        "actor_id": row.fluxer_actor_id,
                        "detail": row.detail,
                        "created_at": row.created_at,
                    }
                    for row in rows
                ],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
