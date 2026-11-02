"""PG 租约驱动的作品任务执行器。"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select, or_, and_, update, func, text
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert
from models.work_task import WorkTask
from models.work import Work
from models.thread import Thread
from models.work_statistics import WorkStatistics
from shared.database import AsyncSessionFactory
from shared.time_utils import utc_now
from identity.auth_runtime import get_auth_service

logger = logging.getLogger(__name__)


class WorkTaskWorker:
    """PG 保留事实及重试，Redis 无数据时也可以恢复全部任务。"""

    def __init__(self, session_factory=AsyncSessionFactory, rpc=None):
        """允许注入会话及平台 RPC 以便验证崩溃恢复。"""
        self.sessions = session_factory
        self.rpc = rpc
        self.runner = None

    async def start(self):
        """创建唯一后台轮询任务。"""
        if self.runner is None:
            self.runner = asyncio.create_task(self.run())

    async def close(self):
        """停止轮询，未完成租约由下一进程恢复。"""
        if self.runner:
            self.runner.cancel()
            try:
                await self.runner
            except asyncio.CancelledError:
                pass
            self.runner = None

    async def run(self):
        """持续处理任务，单次失败不会停止消费。"""
        while True:
            try:
                handled = await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("作品任务轮询失败")
                handled = False
            if not handled:
                await asyncio.sleep(2)

    async def once(self):
        """提交领取租约后再调用网络，持锁期间不等待平台。"""
        from identity.runtime import request_identity_mode

        request_identity_mode.set("fluxer")
        now = utc_now()
        async with self.sessions() as session:
            earlier = aliased(WorkTask)
            blocked_move = (
                select(earlier.id)
                .where(
                    earlier.work_id == WorkTask.work_id,
                    earlier.kind == "move",
                    earlier.id < WorkTask.id,
                    earlier.status.in_(["pending", "running"]),
                )
                .exists()
            )
            # 同帖较早编辑仍可能重试时，不允许较新编辑先执行再被旧值覆盖。
            blocked_edit = (
                select(earlier.id)
                .where(
                    earlier.work_id == WorkTask.work_id,
                    earlier.kind == "edit_thread",
                    earlier.id < WorkTask.id,
                    earlier.payload["thread_id"].as_string()
                    == WorkTask.payload["thread_id"].as_string(),
                    earlier.status.in_(["pending", "running"]),
                )
                .exists()
            )
            task = (
                (
                    await session.execute(
                        select(WorkTask)
                        .where(
                            or_(
                                and_(
                                    WorkTask.status == "pending",
                                    WorkTask.available_at <= now,
                                ),
                                and_(
                                    WorkTask.status == "running",
                                    WorkTask.lease_until <= now,
                                ),
                            )
                        )
                        .where(or_(WorkTask.kind != "move", ~blocked_move))
                        .where(or_(WorkTask.kind != "edit_thread", ~blocked_edit))
                        .order_by(WorkTask.id)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if task is None:
                from work.work_moderation_service import WorkModerationService

                await WorkModerationService(session).expire()
                await session.commit()
                return False
            if task.kind == "move" and task.dispatched_at is None:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:id)"), {"id": task.work_id}
                )
                current = await session.get(Work, task.work_id, populate_existing=True)
                if (
                    task.payload.get("classification_revision", 0)
                    != current.classification_revision
                ):
                    task.status = "done"
                    task.error = "SUPERSEDED"
                    await session.commit()
                    return True
            already_dispatched = task.dispatched_at is not None
            if task.dispatched_at is None and task.kind != "create_child":
                task.dispatched_at = now
            task.status = "running"
            task.attempts += 1
            task.lease_until = now + timedelta(seconds=90)
            identifier = task.id
            work_id = task.work_id
            kind = task.kind
            payload = dict(task.payload)
            attempt = task.attempts
            await session.commit()
        try:
            if kind == "create_child" and not already_dispatched:
                payload = await self.prepare_child_dispatch(identifier, work_id, attempt)
            result = await self.execute(identifier, work_id, kind, payload)
            async with self.sessions() as session:
                task = await session.get(WorkTask, identifier, with_for_update=True)
                # 仅租约当前持有者提交完成，过期执行者不能覆盖新一轮重试。
                if task.status == "running" and task.attempts == attempt:
                    await self.apply_result(session, task, result)
                    task.status = "done"
                    task.lease_until = None
                    task.error = None
                    await session.commit()
        except Exception as exc:
            async with self.sessions() as session:
                task = await session.get(WorkTask, identifier, with_for_update=True)
                if task.status == "running" and task.attempts == attempt:
                    from identity.integration_rpc_error import IntegrationRpcError

                    task.status = (
                        "failed"
                        if isinstance(exc, IntegrationRpcError) and not exc.retryable
                        else "pending"
                    )
                    task.error = (
                        exc.code
                        if isinstance(exc, IntegrationRpcError)
                        else type(exc).__name__
                    )
                    task.lease_until = None
                    task.available_at = utc_now() + timedelta(
                        seconds=min(3600, 2 ** min(attempt, 11))
                    )
                    await session.commit()
        return True

    async def prepare_child_dispatch(self, identifier, work_id, attempt):
        """首发采用最新分类，原子持久化目标及发送标记后才允许原生创建。"""
        from api.v2.config import get_work_config
        from identity.integration_rpc_error import IntegrationRpcError
        from models.discord_identity import DiscordIdentity
        from shared.abyss_tag_visibility import can_view_abyss_tags
        from work.work_permission_service import WorkPermissionService
        from work.work_moderation_service import WorkModerationService

        config = get_work_config()
        async with self.sessions() as session:
            task = await session.get(WorkTask, identifier)
            current = await session.get(Work, work_id)
            if current is None or current.deleted_at or not current.source_visible:
                raise IntegrationRpcError("CHILD_WORK_UNAVAILABLE", False)
            revision = current.classification_revision
            abyss = current.is_abyss
            if current.source_platform == "discord" and config.get("abyss") is not None:
                abyss = current.source_channel_id in {
                    str(value) for value in config["abyss"].get("channel_ids", [])
                }
            classification = (revision, current.is_nsfw, abyss, current.source_channel_id)
            key = ("abyss" if abyss else "normal") + ("_nsfw" if current.is_nsfw else "")
            forums = config.get("work_forums", {})
            target, guild = forums.get(key), forums.get("guild_id")
            actor_id = task.payload["actor_id"]
            binding = (
                await session.execute(
                    select(DiscordIdentity.discord_user_id, DiscordIdentity.version).where(
                        DiscordIdentity.user_id == actor_id,
                        DiscordIdentity.is_current.is_(True),
                    )
                )
            ).one_or_none()
            if binding is None:
                raise IntegrationRpcError("CHILD_ACTOR_INELIGIBLE", False)
            discord_id, binding_version = binding
        if not target or not guild:
            raise IntegrationRpcError("CHILD_TARGET_UNAVAILABLE", True)
        if not await get_auth_service().legacy_eligibility(
            discord_id, actor_id, binding_version, force_authority=True
        ):
            raise IntegrationRpcError("CHILD_ACTOR_INELIGIBLE", False)
        # 在数据库事务之外核验当前资格，不冒用管理身份进入受限分类。
        permissions = WorkPermissionService(config)
        try:
            try:
                member = await permissions.member(actor_id, discord_id, binding_version, sensitive=True)
            except HTTPException as exc:
                if exc.status_code == 403:
                    raise IntegrationRpcError("CHILD_ACTOR_INELIGIBLE", False) from exc
                raise
        finally:
            await permissions.close()
        roles = {str(value) for value in member["roles"]}
        required = config.get("auth", {}).get("role_ids", [])
        required = required.split(",") if isinstance(required, str) else required
        if not roles.intersection(str(value).strip() for value in required) or (
            abyss and not can_view_abyss_tags(roles, config.get("abyss"))
        ):
            raise IntegrationRpcError("CHILD_ACTOR_INELIGIBLE", False)
        deadline = member.get("communication_disabled_until")
        try:
            muted = bool(deadline) and datetime.fromisoformat(deadline.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except (ValueError, TypeError) as exc:
            raise IntegrationRpcError("CHILD_ACTOR_STATE_UNKNOWN", True) from exc
        async with self.sessions() as session:
            # 与身份投影、任务结果及帖子触发器保持一致锁序，避免等待期间换绑。
            for key in ("identity-merge:" + actor_id, "binding:" + actor_id):
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})
            latest_binding = (
                await session.execute(
                    select(DiscordIdentity.discord_user_id, DiscordIdentity.version).where(
                        DiscordIdentity.user_id == actor_id,
                        DiscordIdentity.is_current.is_(True),
                    ).with_for_update()
                )
            ).one_or_none()
            if latest_binding is None or tuple(latest_binding) != (discord_id, binding_version):
                raise IntegrationRpcError("CHILD_BINDING_CHANGED", True)
            task = await session.get(WorkTask, identifier, with_for_update=True)
            if task.status != "running" or task.attempts != attempt or task.dispatched_at is not None:
                raise IntegrationRpcError("CHILD_LEASE_CHANGED", True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": work_id})
            main = (
                await session.execute(
                    select(Thread).where(Thread.work_id == work_id, Thread.role == "main").with_for_update()
                )
            ).scalars().first()
            current = await session.get(Work, work_id, populate_existing=True, with_for_update=True)
            latest_abyss = current.is_abyss
            if current.source_platform == "discord" and config.get("abyss") is not None:
                latest_abyss = current.source_channel_id in {
                    str(value) for value in config["abyss"].get("channel_ids", [])
                }
            latest_classification = (
                current.classification_revision, current.is_nsfw, latest_abyss, current.source_channel_id
            )
            if latest_classification != classification:
                raise IntegrationRpcError("CHILD_CLASSIFICATION_CHANGED", True)
            if current.deleted_at or not current.source_visible:
                raise IntegrationRpcError("CHILD_WORK_UNAVAILABLE", False)
            if main is None or main.deleted_at or main.archived or main.archive_owner_ids or main.lock_owner_ids or main.platform_locked:
                raise IntegrationRpcError("CHILD_MAIN_UNAVAILABLE", False)
            try:
                await WorkModerationService(session).require_speech(current, {"id": actor_id, "can_speak": not muted})
            except HTTPException as exc:
                if exc.status_code == 403:
                    raise IntegrationRpcError("CHILD_ACTOR_INELIGIBLE", False) from exc
                raise
            task.payload = {**task.payload, "guild_id": str(guild), "channel_id": str(target), "classification_revision": revision}
            task.dispatched_at = utc_now()
            payload = dict(task.payload)
            await session.commit()
            return payload

    async def execute(self, identifier, work_id, kind, payload):
        """以持久任务编号作为平台幂等键，不写入用户会话。"""
        if kind == "projection":
            return {}
        rpc = self.rpc or get_auth_service().rpc
        operation_id = "forum_work_operation_" + str(identifier)
        common = {"operation_id": operation_id, "actor_user_id": payload["actor_id"]}
        if kind == "managed_rebind":
            return await rpc(
                "rpc.identity.v1.rebind_service",
                **common,
                user_id=payload["user_id"],
                discord_user_id=payload["discord_user_id"],
                reason=payload["reason"],
                profile=payload["profile"],
            )
        if kind == "move":
            if payload["platform"] == "discord":
                raise RuntimeError("Discord 来源不会在 Fluxer 原生平台执行移动")
            return await rpc(
                "rpc.content.v1.thread.move_service",
                **common,
                guild_id=payload["guild_id"],
                thread_id=payload["thread_id"],
                target_channel_id=payload["target_channel_id"],
            )
        if kind in ("create_main", "create_child"):
            if kind == "create_child":
                from work.work_moderation_service import WorkModerationService

                async with self.sessions() as session:
                    work = await session.get(Work, work_id)
                    await WorkModerationService(session).require_speech(
                        work, {"id": payload["actor_id"], "can_speak": True}
                    )
                    pending = (
                        await session.execute(
                            select(WorkTask.id)
                            .where(
                                WorkTask.work_id == work_id,
                                WorkTask.kind == "policy",
                                WorkTask.status.in_(["pending", "running"]),
                                WorkTask.payload["source"].as_string()
                                == "forum_work_mute",
                            )
                            .limit(1)
                        )
                    ).first()
                    if pending:
                        raise RuntimeError("作品禁言策略正在同步")
            return await rpc(
                "rpc.content.v1.thread.create_service",
                **common,
                guild_id=payload["guild_id"],
                channel_id=payload["channel_id"],
                name=payload["title"],
                content=payload["body"]
                or ("" if payload.get("attachments") else payload["title"]),
                **(
                    {"attachments": payload["attachments"]}
                    if "attachments" in payload
                    else {}
                ),
                **(
                    {"initial_policies": payload["initial_policies"]}
                    if "initial_policies" in payload
                    else {}
                ),
                **({"defer_open": True} if payload.get("defer_open") else {}),
                **(
                    {"parent_thread_id": payload["parent_thread_id"]}
                    if payload.get("parent_thread_id")
                    else {}
                ),
            )
        if kind == "policy" and payload.get("depends_on"):
            async with self.sessions() as session:
                blocked = (
                    await session.execute(
                        select(WorkTask.id)
                        .where(
                            WorkTask.id.in_(payload["depends_on"]),
                            WorkTask.status != "done",
                        )
                        .limit(1)
                    )
                ).first()
                if blocked:
                    from identity.integration_rpc_error import IntegrationRpcError

                    raise IntegrationRpcError("INITIALIZATION_DEPENDENCY_PENDING", True)
        if kind == "policy":
            return await rpc(
                "rpc.content.v1.thread.policy_service",
                **common,
                guild_id=payload["guild_id"],
                thread_id=payload["thread_id"],
                source=payload["source"],
                policy_version=identifier,
                kind="deny_send" if payload.get("target_user_id") else "lock",
                active=payload["active"],
                expires_at=payload.get("expires_at"),
                **(
                    {"target_user_id": payload["target_user_id"]}
                    if payload.get("target_user_id")
                    else {}
                ),
            )
        if kind == "edit_thread":
            result = {}
            if "title" in payload or "archived" in payload:
                changes = {
                    key: payload[source]
                    for source, key in (("title", "name"), ("archived", "archived"))
                    if source in payload
                }
                result = await rpc(
                    "rpc.content.v1.thread.update_service",
                    operation_id=operation_id + "_metadata",
                    actor_user_id=payload["actor_id"],
                    guild_id=payload["guild_id"],
                    thread_id=payload["thread_id"],
                    **changes,
                )
            if "body" in payload or "attachments" in payload:
                result = await rpc(
                    "rpc.content.v1.starter.edit_service",
                    operation_id=operation_id + "_starter",
                    actor_user_id=payload["actor_id"],
                    guild_id=payload["guild_id"],
                    thread_id=payload["thread_id"],
                    **({"content": payload["body"]} if "body" in payload else {}),
                    **(
                        {"attachments": payload["attachments"]}
                        if "attachments" in payload
                        else {}
                    ),
                )
            if "attachments" in payload:
                snapshot = await rpc(
                    "rpc.content.v1.thread.read_service",
                    operation_id=operation_id + "_mirror",
                    actor_user_id=payload["actor_id"],
                    guild_id=payload["guild_id"],
                    thread_id=payload["thread_id"],
                )
                result = {**result, "_native_message": snapshot["message"]}
            return result
        raise ValueError("未知作品任务类型")

    async def apply_result(self, session, task, result):
        """平台结果与任务完成在同一 PG 事务提交。"""
        if task.kind == "managed_rebind":
            from identity.binding_projection_service import BindingProjectionService

            event = {
                **result,
                "actor_user_id": task.payload["actor_id"],
                "operation_id": "forum_work_operation_" + str(task.id),
                "kind": "managed_merge"
                if result.get("merged_user_id")
                else "binding_changed",
            }
            await BindingProjectionService(session).apply(event)
        elif task.kind == "projection":
            counts = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(Thread.reaction_count), 0),
                        func.coalesce(func.sum(Thread.reply_count), 0),
                    ).where(
                        Thread.work_id == task.work_id,
                        Thread.role == "main",
                        Thread.deleted_at.is_(None),
                    )
                )
            ).one()
            statement = insert(WorkStatistics).values(
                work_id=task.work_id, reaction_count=counts[0], reply_count=counts[1]
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[WorkStatistics.work_id],
                    set_={"reaction_count": counts[0], "reply_count": counts[1]},
                )
            )
        elif task.kind in ("create_main", "create_child"):
            # 先串行作品，再写会触发作品投影的帖子，避免与分类事务反序等待。
            await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": task.work_id})
            payload = task.payload
            external = int(result["thread_id"])
            existing = (
                (
                    await session.execute(
                        select(Thread).where(
                            Thread.platform == "fluxer", Thread.thread_id == external
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is None:
                session.add(
                    Thread(
                        initializing=bool(payload.get("defer_open")),
                        platform="fluxer",
                        thread_id=external,
                        guild_id=int(result["guild_id"]),
                        channel_id=int(result["channel_id"]),
                        work_id=task.work_id,
                        role="main" if task.kind == "create_main" else "child",
                        title=payload["title"],
                        body=payload["body"],
                        first_message_excerpt=payload["body"][:2000],
                        thumbnail_urls=payload.get("images", []),
                        fluxer_author_id=payload["actor_id"],
                        author_id=None,
                    )
                )
            await session.flush()
            child = (
                (
                    await session.execute(
                        select(Thread).where(
                            Thread.platform == "fluxer", Thread.thread_id == external
                        )
                    )
                )
                .scalars()
                .one()
            )
            from work.work_moderation_service import WorkModerationService

            await WorkModerationService(session).reconcile_child(child, payload)
            if task.kind == "create_main":
                work = await session.get(Work, task.work_id)
                work.source_guild_id = str(result["guild_id"])
                work.source_channel_id = str(result["channel_id"])
                work.source_thread_id = str(result["thread_id"])
                from shared.platform_thread_ref import fluxer_thread_ref

                work.main_ref = fluxer_thread_ref(
                    work.source_guild_id, work.source_thread_id
                )

        elif task.kind == "edit_thread":
            thread = (
                (
                    await session.execute(
                        select(Thread).where(
                            Thread.platform == "fluxer",
                            Thread.thread_id == int(task.payload["thread_id"]),
                        )
                    )
                )
                .scalars()
                .one()
            )
            for key in ("title", "body", "archived"):
                if key in task.payload:
                    setattr(thread, key, task.payload[key])
            if "_native_message" in result:
                from shared.image_url_utils import extract_message_image_urls

                message = result["_native_message"]
                thread.body = message.get("content") or ""
                thread.first_message_excerpt = thread.body[:2000]
                thread.thumbnail_urls = extract_message_image_urls(
                    attachments=message.get("attachments", []),
                    embeds=message.get("embeds", []),
                    content=thread.body,
                )
        elif task.kind == "move":
            await session.execute(
                update(Thread)
                .where(
                    Thread.platform == task.payload["platform"],
                    Thread.thread_id == int(task.payload["thread_id"]),
                )
                .values(channel_id=int(result["channel_id"]))
            )
