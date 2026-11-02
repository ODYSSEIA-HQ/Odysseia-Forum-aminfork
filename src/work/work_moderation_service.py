"""作品范围的处罚、软删恢复与原生初始化事务。"""

import uuid
from fastapi import HTTPException
from sqlalchemy import select, or_, text
from models.work import Work
from models.thread import Thread
from models.work_task import WorkTask
from models.work_user_mute import WorkUserMute
from models.operation_log import OperationLog
from shared.time_utils import utc_now


class WorkModerationService:
    """处罚与可靠任务共同提交，原生锁来源互不覆盖。"""

    def __init__(self, session):
        """复用请求或后台任务的会话。"""
        self.session = session

    async def require_speech(self, work, principal):
        """禁言仅限制发言及建子帖，不影响收藏、标签和反应。"""
        restricted = (
            await self.session.execute(
                select(WorkUserMute.id)
                .where(
                    WorkUserMute.work_id == work.id,
                    WorkUserMute.user_id == principal["id"],
                    WorkUserMute.revoked_at.is_(None),
                    or_(
                        WorkUserMute.expires_at.is_(None),
                        WorkUserMute.expires_at > utc_now(),
                    ),
                )
                .limit(1)
            )
        ).first()
        if not principal["can_speak"] or restricted:
            raise HTTPException(403, "当前作品禁止发言和创建子帖")

    async def policy_tasks(
        self,
        work_id,
        principal,
        source,
        active,
        threads=None,
        target_user_id=None,
        expires_at=None,
    ):
        """只向Fluxer讨论区投影，绝不操作Discord主帖。"""
        if threads is None:
            threads = (
                (
                    await self.session.execute(
                        select(Thread).where(
                            Thread.work_id == work_id, Thread.platform == "fluxer"
                        )
                    )
                )
                .scalars()
                .all()
            )
        tasks = []
        for thread in threads:
            task = WorkTask(
                work_id=work_id,
                kind="policy",
                dedupe_key="moderation:" + uuid.uuid4().hex,
                payload={
                    "actor_id": principal["id"],
                    "guild_id": str(thread.guild_id),
                    "thread_id": str(thread.thread_id),
                    "source": source,
                    "active": active,
                    "target_user_id": target_user_id,
                    "expires_at": expires_at.isoformat() + "Z" if expires_at else None,
                },
            )
            self.session.add(task)
            tasks.append(task)
        await self.session.flush()
        return tasks

    async def soft_delete(self, work, principal, deleted):
        """普通隐藏可管理恢复，源主帖权威删除不可恢复。"""
        if not deleted and (
            not principal["is_admin"] or work.deletion_reason == "source_deleted"
        ):
            raise HTTPException(403, "此作品不可恢复")
        if deleted and not (principal["is_admin"] or work.author_id == principal["id"]):
            raise HTTPException(403, "需要作者或管理权限")
        if not deleted and work.deletion_reason == "index_removed":
            sources = (
                (
                    await self.session.execute(
                        select(Thread).where(
                            Thread.work_id == work.id,
                            Thread.role == "main",
                            Thread.deletion_reason == "index_removed",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for source in sources:
                source.deleted_at = None
                source.deletion_reason = None
                source.show_flag = True
            work.source_visible = True
        work.deleted_at = utc_now() if deleted else None
        work.deletion_reason = "moderation" if deleted else None
        await self.policy_tasks(work.id, principal, "forum_soft_delete", deleted)
        self.session.add(
            OperationLog(
                type="work.delete" if deleted else "work.restore",
                target_type="work",
                target_id=work.id,
                actor_id=int(principal["discord_user_id"]),
                fluxer_actor_id=principal["id"],
                detail={"reason": work.deletion_reason},
            )
        )
        await self.session.flush()

    async def effective_mute(self, work_id, user_id):
        """多处罚来源取并集，永久来源优先于有期限来源。"""
        rows = (
            (
                await self.session.execute(
                    select(WorkUserMute).where(
                        WorkUserMute.work_id == work_id,
                        WorkUserMute.user_id == user_id,
                        WorkUserMute.revoked_at.is_(None),
                        or_(
                            WorkUserMute.expires_at.is_(None),
                            WorkUserMute.expires_at > utc_now(),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        deadline = (
            None
            if any(row.expires_at is None for row in rows)
            else max((row.expires_at for row in rows), default=None)
        )
        return bool(rows), deadline

    async def mute(
        self, work, principal, user_id, reason, expires_at=None, restriction_id=None
    ):
        """同操作者只撤自己的处罚来源，所有变更可审计。"""
        if not (principal["is_admin"] or work.author_id == principal["id"]):
            raise HTTPException(403, "需要作者或管理权限")
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:work)"), {"work": work.id}
        )
        if restriction_id is not None:
            row = await self.session.get(
                WorkUserMute, restriction_id, with_for_update=True
            )
            if row is None or row.work_id != work.id:
                raise HTTPException(404, "处罚不存在")
            actor = (
                await self.session.execute(
                    text("SELECT canonical_forum_user(:id)"), {"id": row.actor_id}
                )
            ).scalar_one_or_none()
            if (actor or row.actor_id) != principal["id"]:
                raise HTTPException(403, "只能撤销自己的处罚来源")
            user_id = row.user_id
            row.revoked_at = utc_now()
        else:
            if not isinstance(reason, str) or not reason.strip() or not user_id:
                raise HTTPException(422, "需要目标用户和处罚原因")
            row = (
                (
                    await self.session.execute(
                        select(WorkUserMute)
                        .where(
                            WorkUserMute.work_id == work.id,
                            WorkUserMute.user_id == user_id,
                            WorkUserMute.actor_id == principal["id"],
                            WorkUserMute.revoked_at.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = WorkUserMute(
                    work_id=work.id,
                    user_id=user_id,
                    actor_id=principal["id"],
                    reason=reason,
                    expires_at=expires_at,
                )
                self.session.add(row)
            else:
                row.reason = reason
                row.expires_at = expires_at
        await self.session.flush()
        active, deadline = await self.effective_mute(work.id, user_id)
        tasks = await self.policy_tasks(
            work.id,
            principal,
            "forum_work_mute",
            active,
            target_user_id=user_id,
            expires_at=deadline,
        )
        self.session.add(
            OperationLog(
                type="work.mute_revoke" if restriction_id else "work.mute",
                target_type="work",
                target_id=work.id,
                actor_id=int(principal["discord_user_id"]),
                fluxer_actor_id=principal["id"],
                detail={
                    "mute_id": str(row.id),
                    "user_id": user_id,
                    "reason": row.reason,
                    "expires_at": row.expires_at.isoformat()
                    if row.expires_at
                    else None,
                },
            )
        )
        return {
            "id": str(row.id),
            "work_id": str(work.id),
            "user_id": row.user_id,
            "actor_id": row.actor_id,
            "reason": row.reason,
            "expires_at": row.expires_at,
            "revoked_at": row.revoked_at,
            "task_ids": [str(task.id) for task in tasks],
        }

    async def initial_policies(self, work_id, version):
        """完整冻结有效处罚，超过100个目标明确拒绝而不截断。"""
        rows = (
            (
                await self.session.execute(
                    select(WorkUserMute).where(
                        WorkUserMute.work_id == work_id,
                        WorkUserMute.revoked_at.is_(None),
                        or_(
                            WorkUserMute.expires_at.is_(None),
                            WorkUserMute.expires_at > utc_now(),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        users = {row.user_id for row in rows}
        if len(users) > 100:
            raise HTTPException(
                409, "当前有效禁言目标超过100，无法安全继承；请减少有效处罚后重试"
            )
        result = []
        for user_id in sorted(users):
            matching = [row for row in rows if row.user_id == user_id]
            deadline = (
                None
                if any(row.expires_at is None for row in matching)
                else max(row.expires_at for row in matching)
            )
            result.append(
                {
                    "source": "forum_work_mute",
                    "policy_version": version,
                    "target_user_id": user_id,
                    "active": True,
                    "expires_at": deadline.isoformat() + "Z" if deadline else None,
                }
            )
        return result

    async def reconcile_child(self, thread, creation_payload):
        """锁内冻结最新处罚任务及开放依赖，任何失败都保持原生初始化锁。"""
        if (
            thread.platform != "fluxer"
            or thread.role != "child"
            or not creation_payload.get("defer_open")
        ):
            return
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": thread.work_id}
        )
        guard_key = "release-child:" + str(thread.id)
        if (
            await self.session.execute(
                select(WorkTask.id).where(WorkTask.dedupe_key == guard_key)
            )
        ).first():
            return
        current = (
            (
                await self.session.execute(
                    select(WorkUserMute.user_id).where(
                        WorkUserMute.work_id == thread.work_id
                    )
                )
            )
            .scalars()
            .all()
        )
        users = set(current) | {
            item["target_user_id"]
            for item in creation_payload.get("initial_policies", [])
        }
        dependencies = []
        for user_id in sorted(users):
            active, deadline = await self.effective_mute(thread.work_id, user_id)
            tasks = await self.policy_tasks(
                thread.work_id,
                {"id": creation_payload["actor_id"]},
                "forum_work_mute",
                active,
                threads=[thread],
                target_user_id=user_id,
                expires_at=deadline,
            )
            dependencies.extend(task.id for task in tasks)
        work = await self.session.get(Work, thread.work_id)
        if work.deleted_at:
            tasks = await self.policy_tasks(
                thread.work_id,
                {"id": creation_payload["actor_id"]},
                "forum_soft_delete",
                True,
                threads=[thread],
            )
            dependencies.extend(task.id for task in tasks)
        self.session.add(
            WorkTask(
                work_id=thread.work_id,
                kind="policy",
                dedupe_key=guard_key,
                payload={
                    "actor_id": creation_payload["actor_id"],
                    "guild_id": str(thread.guild_id),
                    "thread_id": str(thread.thread_id),
                    "source": "forum_initializing",
                    "active": False,
                    "depends_on": dependencies,
                },
            )
        )
        await self.session.flush()

    async def expire(self):
        """到期后可靠释放聚合处罚，保持来源记录可追溯。"""
        work_ids = (
            (
                await self.session.execute(
                    select(WorkUserMute.work_id)
                    .where(
                        WorkUserMute.revoked_at.is_(None),
                        WorkUserMute.expires_at <= utc_now(),
                    )
                    .distinct()
                    .order_by(WorkUserMute.work_id)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        for work_id in work_ids:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:id)"), {"id": work_id}
            )
            rows = (
                (
                    await self.session.execute(
                        select(WorkUserMute)
                        .where(
                            WorkUserMute.work_id == work_id,
                            WorkUserMute.revoked_at.is_(None),
                            WorkUserMute.expires_at <= utc_now(),
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.revoked_at = utc_now()
            await self.session.flush()
            for user_id, actor_id in {(row.user_id, row.actor_id) for row in rows}:
                active, deadline = await self.effective_mute(work_id, user_id)
                actor = (
                    await self.session.execute(
                        text("SELECT canonical_forum_user(:id)"), {"id": actor_id}
                    )
                ).scalar_one_or_none() or actor_id
                await self.policy_tasks(
                    work_id,
                    {"id": actor},
                    "forum_work_mute",
                    active,
                    target_user_id=user_id,
                    expires_at=deadline,
                )
