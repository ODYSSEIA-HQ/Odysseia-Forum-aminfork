"""平台事件去重、版本裁决和作品投影。"""

from sqlalchemy import select, text, delete
from sqlalchemy.dialects.postgresql import insert
from models.thread import Thread
from models.work import Work
from models.work_event import WorkEvent
from models.work_reaction import WorkReaction
from shared.time_utils import utc_now


class WorkEventService:
    """事件和投影在同一事务提交，失败可安全重送。"""

    def __init__(self, session, forums=None):
        """复用调用方事务。"""
        self.session = session
        self.forums = forums or {}

    async def create_mapping(self, event):
        """关联 Forum 创建意图；允许论坛中的原生新帖自动建立主作品。"""
        from models.work_task import WorkTask
        import re

        operation = event.get("creation_operation_id")
        task = None
        if operation:
            match = re.fullmatch(r"forum_work_operation_([0-9]+)", operation)
            if match is None:
                raise ValueError("未知 Forum 创建关联编号")
            task = await self.session.get(WorkTask, int(match.group(1)))
            if task is None or task.kind not in ("create_main", "create_child"):
                raise ValueError("创建意图尚未同步")
        from datetime import datetime, timezone

        created_at = None
        if event.get("created_at"):
            created_at = datetime.fromisoformat(
                str(event["created_at"]).replace("Z", "+00:00")
            )
            if created_at.tzinfo is None:
                raise ValueError("原生创建时间必须包含时区")
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        source_author = str(event.get("author_id") or "")
        canonical = (
            (
                await self.session.execute(
                    text("SELECT canonical_forum_user(:id)"), {"id": source_author}
                )
            ).scalar_one_or_none()
            if source_author
            else None
        )
        if task:
            work = await self.session.get(Work, task.work_id)
            if work is None:
                raise ValueError("创建意图的作品不存在")
            role = "main" if task.kind == "create_main" else "child"
        else:
            channels = {
                str(value)
                for key, value in self.forums.items()
                if key in ("normal", "normal_nsfw", "abyss", "abyss_nsfw")
            }
            if (
                event.get("platform") != "fluxer"
                or str(event.get("guild_id")) != str(self.forums.get("guild_id"))
                or str(event.get("channel_id")) not in channels
            ):
                raise ValueError("事件来源不在已配置原生作品论坛")
            channel = str(event["channel_id"])
            work = Work(
                title=event.get("title") or "",
                description=event.get("content"),
                images=event.get("images", []),
                author_id=canonical or source_author or None,
                source_platform="fluxer",
                source_guild_id=str(event["guild_id"]),
                source_channel_id=channel,
                source_thread_id=str(event["thread_id"]),
                main_ref=__import__(
                    "shared.platform_thread_ref", fromlist=["fluxer_thread_ref"]
                ).fluxer_thread_ref(event["guild_id"], event["thread_id"]),
                is_nsfw=channel
                in {
                    str(self.forums.get("normal_nsfw")),
                    str(self.forums.get("abyss_nsfw")),
                },
                is_abyss=channel
                in {str(self.forums.get("abyss")), str(self.forums.get("abyss_nsfw"))},
            )
            if created_at is not None:
                work.created_at = created_at
            self.session.add(work)
            await self.session.flush()
            role = "main"
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": work.id}
        )
        title = event.get("title") or (task.payload["title"] if task else "")
        thread = Thread(
            initializing=bool(task and task.payload.get("defer_open")),
            platform="fluxer",
            thread_id=int(event["thread_id"]),
            guild_id=int(event["guild_id"]),
            channel_id=int(event["channel_id"]),
            work_id=work.id,
            role=role,
            title=title,
            body=event.get("content") or "",
            first_message_excerpt=(event.get("content") or "")[:2000],
            thumbnail_urls=event.get("images", []),
            fluxer_author_id=canonical or source_author or None,
            source_author_id=source_author or None,
        )
        if created_at is not None:
            thread.created_at = created_at
        self.session.add(thread)
        await self.session.flush()
        if role == "main" and created_at is not None and thread.fluxer_author_id:
            from core.notification_fanout_service import NotificationFanoutService

            await NotificationFanoutService(self.session).fanout_work_created(
                work.id, thread.thread_id, thread.fluxer_author_id, "fluxer", created_at
            )
        if task:
            from work.work_moderation_service import WorkModerationService

            await WorkModerationService(self.session).reconcile_child(
                thread, task.payload
            )
        return thread

    async def apply(self, event):
        """同一作品串行聚合，多线程快照不会相互覆盖。"""
        # 接受固定版本的完整原生快照，事件版本与 RPC 状态版本严格分离。
        envelope = dict(event)
        snapshot = event.get("snapshot", {})
        event = {**event, **snapshot}
        if "body" in snapshot:
            event["content"] = snapshot["body"]
        if snapshot.get("creation_operation_id") and not event.get(
            "creation_operation_id"
        ):
            event["creation_operation_id"] = snapshot["creation_operation_id"]
        emoji_data = {}
        if isinstance(event.get("reactions"), list):
            counts = {}
            for item in event["reactions"]:
                emoji = item["emoji"]
                data = (
                    emoji
                    if isinstance(emoji, dict)
                    else {"id": None, "name": str(emoji), "animated": False}
                )
                key = (
                    "custom:" + str(data["id"])
                    if data.get("id")
                    else "unicode:" + str(data["name"])
                )
                counts[key] = item["count"]
                emoji_data[key] = data
            event["reactions"] = counts
            event["reaction_emojis"] = emoji_data
        if "reaction_count" not in event and "reactions" in event:
            event["reaction_count"] = sum(event["reactions"].values())
        required = ("event_id", "platform", "thread_id", "state_version")
        if (
            any(key not in event for key in required)
            or event["platform"] not in ("discord", "fluxer")
            or not isinstance(event["state_version"], int)
            or event["state_version"] < 1
        ):
            raise ValueError("无效平台事件")
        statement = (
            insert(WorkEvent)
            .values(event_id=event["event_id"], payload=envelope, received_at=utc_now())
            .on_conflict_do_nothing()
            .returning(WorkEvent.event_id)
        )
        accepted = (await self.session.execute(statement)).scalar_one_or_none()
        if accepted is None:
            return False
        from models.work_source_state import WorkSourceState

        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "source:" + event["platform"] + ":" + str(event["thread_id"])},
        )
        state = await self.session.get(
            WorkSourceState,
            (event["platform"], str(event["thread_id"])),
            with_for_update=True,
        )
        if state and (
            event["state_version"] <= state.version
            or (state.deleted and not event.get("deleted"))
        ):
            return False
        if state is None:
            state = WorkSourceState(
                platform=event["platform"],
                thread_id=str(event["thread_id"]),
                version=event["state_version"],
                deleted=bool(event.get("deleted")),
            )
            self.session.add(state)
        else:
            state.version = event["state_version"]
            state.deleted = bool(event.get("deleted")) or state.deleted
        thread = (
            (
                await self.session.execute(
                    select(Thread)
                    .where(
                        Thread.platform == event["platform"],
                        Thread.thread_id == int(event["thread_id"]),
                    )
                    .execution_options(all_platforms=True)
                )
            )
            .scalars()
            .first()
        )
        if thread is None:
            if event.get("deleted"):
                return True
            channels = {
                str(self.forums[key])
                for key in ("normal", "normal_nsfw", "abyss", "abyss_nsfw")
                if self.forums.get(key)
            }
            if (
                channels
                and not event.get("creation_operation_id")
                and (
                    event["platform"] != "fluxer"
                    or str(event.get("guild_id")) != str(self.forums.get("guild_id"))
                    or str(event.get("channel_id")) not in channels
                )
            ):
                return True
            thread = await self.create_mapping(event)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": thread.work_id}
        )
        await self.session.refresh(thread, with_for_update=True)
        if event["state_version"] <= thread.source_version:
            return False
        if event.get("author_id"):
            source_author = str(event["author_id"])
            canonical = (
                await self.session.execute(
                    text("SELECT canonical_forum_user(:id)"), {"id": source_author}
                )
            ).scalar_one_or_none()
            thread.source_author_id = source_author
            event["author_id"] = canonical or source_author
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": thread.work_id}
        )
        old_channel = str(thread.channel_id)
        thread.source_version = event["state_version"]
        if event.get("deleted"):
            thread.deleted_at = utc_now()
            thread.deletion_reason = "source_deleted"
            if thread.role == "main":
                work = await self.session.get(Work, thread.work_id)
                work.deleted_at = utc_now()
                work.deletion_reason = "source_deleted"
        else:
            for source, target in (
                ("title", "title"),
                ("content", "body"),
                ("author_id", "fluxer_author_id"),
                ("archived", "archived"),
                ("native_locked", "platform_locked"),
                ("native_archived", "native_archived"),
                ("images", "thumbnail_urls"),
                ("reaction_count", "reaction_count"),
                ("reply_count", "reply_count"),
                ("reactions", "reactions"),
                ("reaction_emojis", "reaction_emojis"),
            ):
                if source in event:
                    setattr(thread, target, event[source])
            if "content" in event:
                thread.first_message_excerpt = (event["content"] or "")[:2000]
            if "channel_id" in event:
                new_channel = str(event["channel_id"])
                thread.channel_id = int(new_channel)
                if (
                    thread.role == "main"
                    and thread.platform == "fluxer"
                    and new_channel != old_channel
                ):
                    work = await self.session.get(
                        Work, thread.work_id, with_for_update=True
                    )
                    from models.work_task import WorkTask

                    pending = (
                        (
                            await self.session.execute(
                                select(WorkTask).where(
                                    WorkTask.work_id == work.id,
                                    WorkTask.kind == "move",
                                    WorkTask.status.in_(["pending", "running"]),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    newer_desired = any(
                        task.payload.get("classification_revision")
                        == work.classification_revision
                        and task.payload.get("target_channel_id") != new_channel
                        for task in pending
                    )
                    valid_channels = {
                        str(value)
                        for key, value in self.forums.items()
                        if key in ("normal", "normal_nsfw", "abyss", "abyss_nsfw")
                    }
                    if valid_channels:
                        thread.show_flag = new_channel in valid_channels
                        if not newer_desired and new_channel in valid_channels:
                            work.is_nsfw = new_channel in {
                                str(self.forums.get("normal_nsfw")),
                                str(self.forums.get("abyss_nsfw")),
                            }
                            work.is_abyss = new_channel in {
                                str(self.forums.get("abyss")),
                                str(self.forums.get("abyss_nsfw")),
                            }

        from work.work_native_tag_service import WorkNativeTagService

        await WorkNativeTagService(self.session).apply(thread, event)
        await self.session.flush()
        snapshots = (
            await self.session.execute(
                select(Thread.reactions, Thread.reaction_emojis)
                .where(
                    Thread.work_id == thread.work_id,
                    Thread.role == "main",
                    Thread.deleted_at.is_(None),
                )
                .execution_options(all_platforms=True)
            )
        ).all()
        totals = {}
        displays = {}
        for snapshot, metadata in snapshots:
            displays.update(metadata)
            for emoji, count in snapshot.items():
                if not isinstance(count, int) or count < 0:
                    raise ValueError("无效表情聚合")
                totals[emoji] = totals.get(emoji, 0) + count
        await self.session.execute(
            delete(WorkReaction).where(WorkReaction.work_id == thread.work_id)
        )
        if totals:
            await self.session.execute(
                insert(WorkReaction),
                [
                    {
                        "work_id": thread.work_id,
                        "emoji": emoji,
                        "emoji_data": displays.get(emoji, {}),
                        "count": count,
                    }
                    for emoji, count in totals.items()
                ],
            )
        return True
