"""作品业务与批量投影。"""

import hashlib
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from fastapi import HTTPException
from models.work import Work
from models.work_statistics import WorkStatistics
from models.work_reaction import WorkReaction
from models.work_task import WorkTask
from models.tag import Tag
from models.tag_binding import TagBinding
from models.thread import Thread
from models.booklist import Booklist
from models.booklist_item import BooklistItem


class WorkService:
    """协调多表业务，并在事务内持久化任务。"""

    def __init__(self, session):
        """保存请求内数据库会话。"""
        self.session = session

    async def visible(self, work_id, principal, *, owner=False):
        """详情也执行分类过滤，隐藏作品统一返回不存在。"""
        work = await self.session.get(Work, work_id)
        abyss = principal.get("config", {}).get("abyss")
        if work is not None and work.source_platform == "discord" and abyss is not None:
            work.is_abyss = work.source_channel_id in {
                str(value) for value in abyss.get("channel_ids", [])
            }
        if (
            work is None
            or work.deleted_at
            or (
                not work.source_visible
                and not (
                    work.source_platform == "fluxer"
                    and not work.source_thread_id
                    and (work.author_id == principal["id"] or principal["is_admin"])
                )
            )
            or (work.is_nsfw and not principal["show_nsfw"])
            or (
                (
                    work.is_abyss
                    or (
                        work.source_platform == "discord"
                        and work.source_channel_id
                        in {
                            str(value)
                            for value in principal.get("config", {})
                            .get("abyss", {})
                            .get("channel_ids", [])
                        }
                    )
                )
                and not principal["can_view_abyss"]
            )
        ):
            raise HTTPException(404, "作品不存在")
        if owner and work.author_id != principal["id"] and not principal["is_admin"]:
            raise HTTPException(403, "需要作品作者或管理权限")
        return work

    async def project(self, works):
        """每类关联仅一次集合查询，避免列表 N+1。"""
        ids = [work.id for work in works]
        if not ids:
            return []
        statistics = {
            row.work_id: row
            for row in (
                await self.session.execute(
                    select(WorkStatistics).where(WorkStatistics.work_id.in_(ids))
                )
            ).scalars()
        }
        reactions = {value: [] for value in ids}
        for row in (
            await self.session.execute(
                select(WorkReaction).where(WorkReaction.work_id.in_(ids))
            )
        ).scalars():
            reactions[row.work_id].append(
                {
                    "emoji": row.emoji_data
                    or {
                        "id": None,
                        "name": row.emoji.removeprefix("unicode:"),
                        "animated": False,
                    },
                    "count": row.count,
                }
            )
        tags = {value: [] for value in ids}
        for target_id, tag_id, name in await self.session.execute(
            select(TagBinding.work_id, Tag.id, Tag.name)
            .join(Tag, Tag.id == TagBinding.tag_id)
            .where(
                TagBinding.work_id.in_(ids),
                TagBinding.binding_source == "local",
                TagBinding.ended_at.is_(None),
                Tag.deleted_at.is_(None),
            )
        ):
            tags[target_id].append({"id": str(tag_id), "name": name})
        from models.work_native_tag_binding import WorkNativeTagBinding
        from models.work_native_tag_source import WorkNativeTagSource

        native = (
            await self.session.execute(
                select(WorkNativeTagBinding.work_id, Tag.id, Tag.name)
                .join(
                    WorkNativeTagSource,
                    WorkNativeTagSource.id == WorkNativeTagBinding.source_id,
                )
                .join(Tag, Tag.id == WorkNativeTagSource.tag_id)
                .where(WorkNativeTagBinding.work_id.in_(ids), Tag.deleted_at.is_(None))
            )
        ).all()
        for work_id, tag_id, name in native:
            if not any(item["id"] == str(tag_id) for item in tags[work_id]):
                tags[work_id].append(
                    {
                        "id": str(tag_id),
                        "name": name,
                        "readonly": True,
                        "source": "fluxer",
                    }
                )
        result = []
        for work in works:
            data = work.model_dump(
                exclude={
                    "search_vector",
                    "deletion_reason",
                    "metadata_overrides",
                    "classification_revision",
                    "source_visible",
                }
            )
            data["id"] = str(work.id)
            data["sync_status"] = (
                "pending"
                if work.source_platform == "fluxer" and not work.source_thread_id
                else "ready"
            )
            stats = statistics.get(work.id)
            data["statistics"] = {
                key: getattr(stats, key, 0)
                for key in (
                    "view_count",
                    "collection_count",
                    "reaction_count",
                    "reply_count",
                )
            }
            data["reactions"] = reactions[work.id]
            data["tags"] = tags[work.id]
            result.append(data)
        return result

    async def collect(self, work, principal, collected, booklist_id=None):
        """收藏沿用默认书单，统一锁避免旧新入口重复创建默认书单。"""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "default-booklist:" + principal["id"]},
        )
        if booklist_id is not None:
            from core.booklist_repository import BooklistRepository

            resolved = await BooklistRepository(self.session).get_booklist(booklist_id)
            if resolved is None:
                raise HTTPException(404, "书单不存在")
            booklist_id = resolved.id
        query = select(Booklist).where(Booklist.fluxer_owner_id == principal["id"])
        if booklist_id is None:
            query = query.where(Booklist.is_default.is_(True))
        else:
            query = query.where(Booklist.id == booklist_id)
        booklist = (
            (await self.session.execute(query.with_for_update())).scalars().first()
        )
        if booklist is None:
            if booklist_id is not None:
                raise HTTPException(404, "书单不存在")
            booklist = Booklist(
                title="默认收藏",
                is_default=True,
                is_public=False,
                fluxer_owner_id=principal["id"],
                owner_id=int(principal["discord_user_id"]),
            )
            self.session.add(booklist)
            await self.session.flush()
        item = (
            (
                await self.session.execute(
                    select(BooklistItem).where(
                        BooklistItem.booklist_id == booklist.id,
                        BooklistItem.work_id == work.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if collected and item is None:
            main = (
                (
                    await self.session.execute(
                        select(Thread).where(
                            Thread.work_id == work.id,
                            Thread.role == "main",
                            Thread.platform == "discord",
                        )
                    )
                )
                .scalars()
                .first()
            )
            self.session.add(
                BooklistItem(
                    booklist_id=booklist.id,
                    work_id=work.id,
                    thread_id=main.thread_id if main else None,
                    fluxer_owner_id=principal["id"],
                    owner_id=int(principal["discord_user_id"]),
                )
            )
            booklist.item_count += 1
        elif not collected and item is not None:
            await self.session.delete(item)
            booklist.item_count = max(0, booklist.item_count - 1)
        await self.session.flush()
        return {"collected": collected}

    async def view(self, work, principal, redis):
        """Redis 以用户和作品去重二十四小时，统计直接提交 PG。"""
        key = (
            "work:view:"
            + str(work.id)
            + ":"
            + hashlib.sha256(principal["id"].encode()).hexdigest()
        )
        counted = await redis.set(key, "1", nx=True, ex=86400)
        if not counted:
            return {"counted": False}
        try:
            statement = insert(WorkStatistics).values(work_id=work.id, view_count=1)
            await self.session.execute(
                statement.on_conflict_do_update(
                    index_elements=[WorkStatistics.work_id],
                    set_={"view_count": WorkStatistics.view_count + 1},
                )
            )
            await self.session.commit()
        except Exception:
            await redis.delete(key)
            raise
        return {"counted": True}

    async def update(self, work, changes, principal, forums):
        """分类持久期望状态，原生主帖编辑与移动都以可靠任务提交。"""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": work.id}
        )
        await self.session.refresh(work, with_for_update=True)
        abyss = principal.get("config", {}).get("abyss")
        if work.source_platform == "discord" and abyss is not None:
            work.is_abyss = work.source_channel_id in {
                str(value) for value in abyss.get("channel_ids", [])
            }
        if (
            work.source_platform == "discord"
            and "is_abyss" in changes
            and changes["is_abyss"] != work.is_abyss
        ):
            raise HTTPException(403, "Discord 来源深渊分类不能手动修改")
        classification = any(
            key in changes and changes[key] != getattr(work, key)
            for key in ("is_nsfw", "is_abyss")
        )
        if "title" in changes and len(changes["title"]) > 100:
            raise HTTPException(422, "原生作品标题最多100字符")
        if (
            work.source_platform == "fluxer"
            and changes.get("description")
            and len(changes["description"]) > 4000
        ):
            raise HTTPException(422, "原生作品正文最多4000字符")
        main = None
        if work.source_platform == "fluxer" and any(
            key in changes for key in ("title", "description", "attachments")
        ):
            main = (
                (
                    await self.session.execute(
                        select(Thread).where(
                            Thread.work_id == work.id, Thread.role == "main"
                        )
                    )
                )
                .scalars()
                .first()
            )
            if main is None:
                raise HTTPException(409, "主帖仍在创建中")
            from work.work_moderation_service import WorkModerationService

            await WorkModerationService(self.session).require_speech(work, principal)
            if main.platform_locked or main.lock_owner_ids or main.archived:
                raise HTTPException(403, "主帖已锁定或归档，不能编辑")
        if work.source_platform == "fluxer" and "images" in changes:
            raise HTTPException(
                422, "Fluxer作品图片必须通过原生附件预上传和attachments编辑"
            )
        if work.source_platform != "fluxer" and "attachments" in changes:
            raise HTTPException(409, "Discord来源作品不接受Fluxer主帖附件")
        if work.source_platform == "discord":
            local_fields = set(changes).intersection({"title", "description", "images"})
            work.metadata_overrides = sorted(
                set(work.metadata_overrides) | local_fields
            )
        for key, value in changes.items():
            if key == "attachments" or (
                work.source_platform == "fluxer" and key in ("title", "description")
            ):
                continue
            setattr(work, key, value)
        if classification:
            work.classification_revision += 1
            forum_key = ("abyss" if work.is_abyss else "normal") + (
                "_nsfw" if work.is_nsfw else ""
            )
            target = forums.get(forum_key)
            if not target:
                raise HTTPException(503, "目标论坛尚未配置")
            threads = (
                (
                    await self.session.execute(
                        select(Thread).where(
                            Thread.work_id == work.id, Thread.deleted_at.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for thread in threads:
                if thread.platform != "fluxer":
                    continue
                self.session.add(
                    WorkTask(
                        work_id=work.id,
                        kind="move",
                        dedupe_key="move:" + __import__("uuid").uuid4().hex,
                        payload={
                            "platform": thread.platform,
                            "classification_revision": work.classification_revision,
                            "guild_id": str(thread.guild_id),
                            "thread_id": str(thread.thread_id),
                            "target_channel_id": str(target),
                            "actor_id": principal["id"],
                        },
                    )
                )
        if main:
            payload = {
                "actor_id": principal["id"],
                "guild_id": str(main.guild_id),
                "thread_id": str(main.thread_id),
            }
            if "title" in changes:
                payload["title"] = changes["title"]
            if "description" in changes:
                payload["body"] = changes["description"] or ""
            if "attachments" in changes:
                payload["attachments"] = changes["attachments"]
            self.session.add(
                WorkTask(
                    work_id=work.id,
                    kind="edit_thread",
                    dedupe_key="main-edit:" + __import__("uuid").uuid4().hex,
                    payload=payload,
                )
            )
        return work
