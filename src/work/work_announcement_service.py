"""作品公告领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from sqlalchemy import select, func, literal, or_, and_
from sqlalchemy.dialects.postgresql import insert
from shared.time_utils import utc_now
from models.work_announcement import WorkAnnouncement
from work.work_service import WorkService


class WorkAnnouncementService:
    """公告历史与关注者快照共用发布事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def announcements(
        self, work_id: int, command: WorkCommandQo, offset: int = 0, limit: int = 20
    ):
        """历史公告可读，不为新关注者补发通知。"""
        principal = command.actor
        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal)
            offset, limit = max(0, offset), min(100, max(1, limit))
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkAnnouncement)
                    .where(WorkAnnouncement.work_id == work_id)
                )
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(WorkAnnouncement)
                        .where(WorkAnnouncement.work_id == work_id)
                        .order_by(
                            WorkAnnouncement.created_at.desc(),
                            WorkAnnouncement.id.desc(),
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    {**row.model_dump(), "id": str(row.id), "work_id": str(row.work_id)}
                    for row in rows
                ],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def publish_announcement(self, work_id: int, command: WorkCommandQo):
        """每次手动发布在同一事务中快照关注者并写入独立通知。"""
        principal = command.actor
        body = dict(command.values)
        if not principal["can_speak"]:
            raise HTTPException(403, "当前禁止发言")
        if (
            not isinstance(body.get("body"), str)
            or not body["body"].strip()
            or set(body) - {"body", "title", "images"}
        ):
            raise HTTPException(422, "需要公告正文")
        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal, owner=True)
            row = WorkAnnouncement(
                work_id=work_id,
                author_id=principal["id"],
                title=body.get("title"),
                body=body["body"],
                images=body.get("images", []),
            )
            session.add(row)
            await session.flush()
            from models.thread_follow import ThreadFollow
            from models.notification import Notification
            from models.thread import Thread

            discord_thread = (
                select(Thread.thread_id)
                .where(
                    Thread.work_id == work_id,
                    Thread.platform == "discord",
                    Thread.deleted_at.is_(None),
                    Thread.show_flag.is_(True),
                )
                .order_by(Thread.id)
                .limit(1)
                .scalar_subquery()
            )

            recipients = select(
                ThreadFollow.user_id,
                ThreadFollow.fluxer_user_id,
                literal("work_announcement"),
                literal(row.id),
                literal(work_id),
                literal(row.created_at),
                literal("forum"),
                discord_thread,
            ).where(
                or_(
                    ThreadFollow.work_id == work_id,
                    and_(
                        ThreadFollow.work_id.is_(None),
                        ThreadFollow.thread_id.in_(
                            select(Thread.thread_id).where(
                                Thread.work_id == work_id, Thread.platform == "discord"
                            )
                        ),
                    ),
                ),
                ThreadFollow.active_flag.is_(True),
                ThreadFollow.followed_at <= row.created_at,
                or_(
                    ThreadFollow.fluxer_user_id.is_(None),
                    ThreadFollow.fluxer_user_id != principal["id"],
                ),
                or_(
                    ThreadFollow.user_id.is_(None),
                    ThreadFollow.user_id != int(principal["discord_user_id"]),
                ),
            )
            await session.execute(
                insert(Notification)
                .from_select(
                    [
                        "user_id",
                        "fluxer_user_id",
                        "event_type",
                        "event_source_id",
                        "work_id",
                        "created_at",
                        "event_source_platform",
                        "thread_id",
                    ],
                    recipients,
                )
                .on_conflict_do_nothing()
            )
            result = {
                **row.model_dump(),
                "id": str(row.id),
                "work_id": str(row.work_id),
            }
            await session.commit()
            return result

    async def edit_announcement(
        self, work_id: int, announcement_id: int, command: WorkCommandQo
    ):
        """编辑不通知，撤回保留原公告和原通知关联。"""
        principal = command.actor
        body = dict(command.values)
        if set(body) - {"body", "title", "images"}:
            raise HTTPException(422, "无效公告字段")
        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal, owner=True)
            row = await session.get(WorkAnnouncement, announcement_id)
            if row is None or row.work_id != work_id:
                raise HTTPException(404, "公告不存在")
            if command.operation == "withdraw":
                row.withdrawn_at = utc_now()
            else:
                for key, value in body.items():
                    setattr(row, key, value)
            await session.commit()
        return {"updated": True, "withdrawn": command.operation == "withdraw"}
