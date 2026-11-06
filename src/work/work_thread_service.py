"""原生讨论区领域服务。"""

import hashlib
import re
import uuid

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from sqlalchemy import select
from shared.time_utils import utc_now
from models.thread import Thread
from models.work_task import WorkTask
from work.work_service import WorkService


class WorkThreadService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    @staticmethod
    def valid_native_id(value):
        """校验原生社区或频道编号是否处于有符号长整数范围。"""
        # 拒绝布尔值、非规范字符串和超出数据库范围的编号。
        return (
            isinstance(value, (str, int))
            and not isinstance(value, bool)
            and str(value).isascii()
            and str(value).isdigit()
            and 0 < int(value) <= 9223372036854775807
        )

    @staticmethod
    def main_unavailable(main):
        """判断原生主帖是否未就绪、不可用或禁止创建子帖。"""
        # 统一主帖状态判断，避免不同子帖入口放宽限制。
        return (
            main is None
            or main.deleted_at
            or main.initializing
            or main.archived
            or main.archive_owner_ids
            or main.lock_owner_ids
            or main.platform_locked
        )

    @staticmethod
    def project(row, principal):
        """仅投影已授权帖子，所有平台及数据库标识保留字符串。"""
        return {
            "id": str(row.id),
            "platform": row.platform,
            "thread_id": str(row.thread_id),
            "guild_id": str(row.guild_id),
            "channel_id": str(row.channel_id),
            "role": row.role,
            "title": row.title,
            "body": row.body,
            "archived": row.archived,
            "locked": bool(row.lock_owner_ids)
            or row.platform_locked
            or row.initializing,
            "initializing": row.initializing,
            "own_locked": principal["id"] in row.lock_owner_ids,
            "own_archived": principal["id"] in row.archive_owner_ids,
            "can_restore": row.deletion_reason != "source_deleted",
            "deletion_reason": row.deletion_reason,
            "deleted_at": row.deleted_at,
            "author_id": row.fluxer_author_id,
            "created_at": row.created_at,
            "last_active_at": row.last_active_at,
            "reaction_count": row.reaction_count,
            "reply_count": row.reply_count,
            "collection_count": row.collection_count,
        }

    async def threads(
        self, work_id: int, include_deleted: bool, command: WorkCommandQo
    ):
        """列出作品所有有效来源和子帖。"""
        principal = command.actor
        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal)
            query = select(Thread).where(Thread.work_id == work_id)
            if not (include_deleted and principal["is_admin"]):
                query = query.where(Thread.deleted_at.is_(None))
            rows = (await session.execute(query.order_by(Thread.id))).scalars().all()
            return {"items": [self.project(row, principal) for row in rows]}

    async def create_child(self, work_id: int, command: WorkCommandQo):
        """子帖只能由 Forum 创建，主帖锁归档时拒绝新建。"""
        principal = command.actor
        body = dict(command.values)
        if set(body) - {"title", "body", "client_request_id"}:
            raise HTTPException(422, "无效子帖字段")
        request_id = body.get("client_request_id")
        if request_id is not None and (
            not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id)
        ):
            raise HTTPException(422, "无效创建请求编号")
        dedupe_key = "child:" + (
            hashlib.sha256(
                (str(work_id) + ":" + principal["id"] + ":" + request_id).encode()
            ).hexdigest()
            if request_id
            else uuid.uuid4().hex
        )
        if not principal["can_speak"]:
            raise HTTPException(403, "当前禁言禁止创建子帖")
        if (
            not isinstance(body.get("title"), str)
            or not body["title"].strip()
            or (not isinstance(body.get("body"), str))
        ):
            raise HTTPException(422, "需要标题和正文")
        if (
            len(body["title"]) > 100
            or not body["body"].strip()
            or len(body["body"]) > 4000
        ):
            raise HTTPException(422, "原生标题最多100字符，正文须为1至4000字符")
        async with self.sessions() as session:
            await session.execute(
                __import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(:id)"),
                {"id": work_id},
            )
            work = await WorkService(session).visible(work_id, principal)
            if request_id:
                existing = (
                    (
                        await session.execute(
                            select(WorkTask).where(WorkTask.dedupe_key == dedupe_key)
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing:
                    if (
                        existing.payload.get("title") != body["title"]
                        or existing.payload.get("body") != body["body"]
                    ):
                        raise HTTPException(409, "同一创建请求编号不能用于不同内容")
                    return {"task_id": str(existing.id), "status": existing.status}
            from work.work_moderation_service import WorkModerationService

            await WorkModerationService(session).require_speech(work, principal)
            main = (
                (
                    await session.execute(
                        select(Thread)
                        .where(Thread.work_id == work_id, Thread.role == "main")
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if self.main_unavailable(main):
                raise HTTPException(409, "主帖已锁定或归档")
            task = WorkTask(
                work_id=work_id,
                kind="create_child",
                dedupe_key=dedupe_key,
                payload={
                    "actor_id": principal["id"],
                    "title": body["title"],
                    "body": body["body"],
                    "guild_id": str(principal["forums"].get("guild_id", "")),
                    "channel_id": str(
                        principal["forums"].get(
                            ("abyss" if work.is_abyss else "normal")
                            + ("_nsfw" if work.is_nsfw else ""),
                            "",
                        )
                    ),
                },
            )
            if main.platform == "fluxer":
                task.payload = {**task.payload, "parent_thread_id": str(main.thread_id)}
            if not self.valid_native_id(
                task.payload["guild_id"]
            ) or not self.valid_native_id(task.payload["channel_id"]):
                raise HTTPException(503, "Fluxer 子帖论坛尚未配置")
            session.add(task)
            await session.flush()
            from work.work_moderation_service import WorkModerationService

            policies = await WorkModerationService(session).initial_policies(
                work_id, task.id
            )
            task.payload = {
                **task.payload,
                "initial_policies": policies,
                "defer_open": True,
            }
            result = {"task_id": str(task.id), "status": "pending"}
            await session.commit()
            return result

    async def edit_child(self, work_id: int, thread_id: int, command: WorkCommandQo):
        """作者编辑自己子帖；作品作者只能独立加锁归档他人子帖。"""
        principal = command.actor
        body = dict(command.values)
        if set(body) - {"title", "body", "locked", "archived"}:
            raise HTTPException(422, "无效帖子字段")
        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, principal)
            row = (
                (
                    await session.execute(
                        select(Thread)
                        .where(
                            Thread.id == thread_id,
                            Thread.work_id == work_id,
                            Thread.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                raise HTTPException(404, "帖子不存在")
            author = row.fluxer_author_id == principal["id"]
            if any((key in body for key in ("title", "body"))) and (
                not author or row.role != "child" or row.platform != "fluxer"
            ):
                raise HTTPException(403, "只能编辑自己的 Forum 子帖")
            if any((key in body for key in ("title", "body"))) and (
                not principal["can_speak"]
                or row.lock_owner_ids
                or row.platform_locked
                or row.archived
            ):
                raise HTTPException(403, "当前帖子禁止发言")
            if any((key in body for key in ("locked", "archived"))) and (
                not (
                    author or work.author_id == principal["id"] or principal["is_admin"]
                )
            ):
                raise HTTPException(403, "没有帖子管理权限")
            if any((key in body for key in ("title", "body", "archived"))):
                if row.platform != "fluxer":
                    raise HTTPException(409, "Discord 来源的正文归档由 Discord 管理")
                changes = {
                    key: body[key]
                    for key in ("title", "body", "archived")
                    if key in body
                }
                if any(
                    (
                        key in changes
                        and (
                            not isinstance(changes[key], str)
                            or not changes[key].strip()
                        )
                        for key in ("title", "body")
                    )
                ):
                    raise HTTPException(422, "标题正文必须是非空文本")
                if "archived" in changes:
                    if not isinstance(changes["archived"], bool):
                        raise HTTPException(422, "归档必须为布尔值")
                    owners = set(row.archive_owner_ids)
                    if row.native_archived and (not owners):
                        owners.add("__native__")
                    if changes["archived"]:
                        owners.add(principal["id"])
                    else:
                        owners.discard(principal["id"])
                    row.archive_owner_ids = sorted(owners)
                    changes["archived"] = bool(owners)
                    row.archived = bool(owners)
                session.add(
                    WorkTask(
                        work_id=work_id,
                        kind="edit_thread",
                        dedupe_key="edit:" + __import__("uuid").uuid4().hex,
                        payload={
                            "actor_id": principal["id"],
                            "guild_id": str(row.guild_id),
                            "thread_id": str(row.thread_id),
                            **changes,
                        },
                    )
                )
            if "locked" in body:
                if not isinstance(body["locked"], bool):
                    raise HTTPException(422, "锁定状态必须为布尔值")
                locks = set(row.lock_owner_ids)
                if body["locked"]:
                    locks.add(principal["id"])
                else:
                    locks.discard(principal["id"])
                row.lock_owner_ids = sorted(locks)
                if row.platform == "fluxer":
                    session.add(
                        WorkTask(
                            work_id=work_id,
                            kind="policy",
                            dedupe_key="policy:" + __import__("uuid").uuid4().hex,
                            payload={
                                "actor_id": principal["id"],
                                "guild_id": str(row.guild_id),
                                "thread_id": str(row.thread_id),
                                "source": "forum_work_lock",
                                "active": bool(locks),
                            },
                        )
                    )
            await session.commit()
        return {"updated": True}

    async def delete_child(self, work_id: int, thread_id: int, command: WorkCommandQo):
        """主帖删除软删作品；子帖只允许自己的作者或管理删除。"""
        principal = command.actor
        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, principal)
            row = await session.get(
                Thread, thread_id, with_for_update=True, populate_existing=True
            )
            if row is None or row.work_id != work_id:
                raise HTTPException(404, "帖子不存在")
            if row.fluxer_author_id != principal["id"] and (not principal["is_admin"]):
                raise HTTPException(403, "不能删除他人子帖")
            row.deleted_at = utc_now()
            row.deletion_reason = "moderation"
            # 软删除使用独立原生来源，不伪造操作者施加的普通锁。
            if row.platform == "fluxer":
                session.add(
                    WorkTask(
                        work_id=work_id,
                        kind="policy",
                        dedupe_key="softdelete:" + __import__("uuid").uuid4().hex,
                        payload={
                            "actor_id": principal["id"],
                            "guild_id": str(row.guild_id),
                            "thread_id": str(row.thread_id),
                            "source": "forum_soft_delete",
                            "active": True,
                        },
                    )
                )
            if row.role == "main":
                from work.work_moderation_service import WorkModerationService

                await WorkModerationService(session).soft_delete(work, principal, True)
            await session.commit()
        return {"deleted": True}

    async def restore_child(self, work_id: int, thread_id: int, command: WorkCommandQo):
        """仅管理恢复普通子帖，保留其他人的独立锁。"""
        principal = command.actor
        if not principal["is_admin"]:
            raise HTTPException(403, "需要管理权限")
        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal)
            row = await session.get(
                Thread, thread_id, with_for_update=True, populate_existing=True
            )
            if (
                row is None
                or row.work_id != work_id
                or row.role == "main"
                or (row.deletion_reason == "source_deleted")
            ):
                raise HTTPException(404, "可恢复子帖不存在")
            row.deleted_at = None
            row.deletion_reason = None
            # 所有普通锁保持原样，恢复只撤销本次软删除来源。
            if row.platform == "fluxer":
                # 恢复只撤删除来源，绝不重发可能已陈旧的普通锁快照。
                for source, active in (("forum_soft_delete", False),):
                    session.add(
                        WorkTask(
                            work_id=work_id,
                            kind="policy",
                            dedupe_key="restore:"
                            + source
                            + ":"
                            + __import__("uuid").uuid4().hex,
                            payload={
                                "actor_id": principal["id"],
                                "guild_id": str(row.guild_id),
                                "thread_id": str(row.thread_id),
                                "source": source,
                                "active": active,
                            },
                        )
                    )
            await session.commit()
        return {"restored": True}

    async def read_native_thread(
        self, work_id: int, thread_id: int, command: WorkCommandQo
    ):
        """校验作品可见性后从 Fluxer 读取原生首条消息。"""
        principal = command.actor
        from identity.auth_runtime import get_auth_service

        async with self.sessions() as session:
            await WorkService(session).visible(work_id, principal)
            row = await session.get(Thread, thread_id)
            if row is None or row.work_id != work_id or row.deleted_at:
                raise HTTPException(404, "帖子不存在")
            if row.platform != "fluxer":
                raise HTTPException(409, "请通过 Discord 来源链接查看")
            return await get_auth_service().rpc(
                "rpc.content.v1.thread.read_service",
                operation_id="forum_read_operation_" + __import__("uuid").uuid4().hex,
                actor_user_id=principal["id"],
                guild_id=str(row.guild_id),
                thread_id=str(row.thread_id),
            )
