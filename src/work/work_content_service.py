"""作品内容领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from sqlalchemy import select
from shared.redis_client import RedisManager
from models.work import Work
from models.work_task import WorkTask
from work.work_service import WorkService
from api.v2.schemas.native_attachment import NativeAttachment


class WorkContentService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def create_work(self, command: WorkCommandQo):
        """先提交作品与可靠任务，再异步创建 Fluxer 原生主帖。"""
        principal = command.actor
        body = dict(command.values)
        if not principal["can_speak"]:
            raise HTTPException(403, "当前禁止发言")
        if (
            not isinstance(body.get("title"), str)
            or not body["title"].strip()
            or (not isinstance(body.get("description", ""), str))
            or set(body)
            - {"title", "description", "attachments", "is_nsfw", "is_abyss"}
        ):
            raise HTTPException(422, "无效作品字段")
        if body.get("is_abyss") and (not principal["can_view_abyss"]):
            raise HTTPException(403, "没有深渊分类访问权限")
        key = ("abyss" if body.get("is_abyss") else "normal") + (
            "_nsfw" if body.get("is_nsfw") else ""
        )
        forums = principal["forums"]
        channel = forums.get(key)
        guild = forums.get("guild_id")
        if not channel or not guild:
            raise HTTPException(503, "Fluxer 作品论坛尚未配置")
        if len(body["title"]) > 100 or len(body.get("description", "")) > 4000:
            raise HTTPException(422, "原生标题最多100字符，正文最多4000字符")
        if "images" in body and (
            not isinstance(body["images"], list)
            or any(
                (
                    not isinstance(value, str)
                    or not value.startswith(("https://", "http://"))
                    for value in body["images"]
                )
            )
        ):
            raise HTTPException(422, "作品图片必须是HTTP图片地址列表")
        if any(
            (
                key in body and (not isinstance(body[key], bool))
                for key in ("is_nsfw", "is_abyss")
            )
        ):
            raise HTTPException(422, "分类必须是布尔值")
        try:
            attachments = [
                NativeAttachment.model_validate(item).model_dump(exclude_none=True)
                for item in body.get("attachments", [])
            ]
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "原生附件描述无效") from exc
        if len(attachments) > 10 or any(
            (isinstance(item["id"], str) for item in attachments)
        ):
            raise HTTPException(422, "创建时最多10个新预上传附件，不能引用其他消息附件")
        async with self.sessions() as session:
            work = Work(
                title=body["title"],
                description=body.get("description", "")
                if body.get("attachments")
                else body.get("description") or body["title"],
                images=[],
                author_id=principal["id"],
                metadata_overrides=[],
                source_platform="fluxer",
                source_visible=False,
                source_guild_id=str(guild),
                source_channel_id=str(channel),
                is_nsfw=body.get("is_nsfw", False),
                is_abyss=body.get("is_abyss", False),
            )
            session.add(work)
            await session.flush()
            task = WorkTask(
                work_id=work.id,
                kind="create_main",
                dedupe_key="main:" + str(work.id),
                payload={
                    "actor_id": principal["id"],
                    "title": work.title,
                    "body": work.description or "",
                    "attachments": attachments,
                    "guild_id": str(guild),
                    "channel_id": str(channel),
                },
            )
            session.add(task)
            await session.flush()
            result = {
                "work": (await WorkService(session).project([work]))[0],
                "task_id": str(task.id),
                "status": "pending",
            }
            await session.commit()
            return result

    async def upload_target(
        self, is_nsfw: bool, is_abyss: bool, command: WorkCommandQo
    ):
        """创建预上传仅返回当前成员可使用的已配置原生论坛。"""
        principal = command.actor
        if is_abyss and (not principal["can_view_abyss"]):
            raise HTTPException(403, "没有深渊分类访问权限")
        key = ("abyss" if is_abyss else "normal") + ("_nsfw" if is_nsfw else "")
        forums = principal["forums"]
        if not forums.get("guild_id") or not forums.get(key):
            raise HTTPException(503, "原生作品论坛尚未配置")
        return {
            "platform": "fluxer",
            "guild_id": str(forums["guild_id"]),
            "channel_id": str(forums[key]),
        }

    async def get_work(self, work_id: int, command: WorkCommandQo):
        """返回完整作品投影。"""
        principal = command.actor
        async with self.sessions() as session:
            service = WorkService(session)
            work = await service.visible(work_id, principal)
            return (await service.project([work]))[0]

    async def patch_work(self, work_id: int, command: WorkCommandQo):
        """作者编辑元数据，分类变动创建可靠平台任务。"""
        principal = command.actor
        body = dict(command.values)
        allowed = {
            "title",
            "description",
            "images",
            "attachments",
            "is_nsfw",
            "is_abyss",
        }
        if not body or set(body) - allowed:
            raise HTTPException(422, "无效作品字段")
        if "title" in body and (
            not isinstance(body["title"], str) or not body["title"].strip()
        ):
            raise HTTPException(422, "标题不能为空")
        if (
            "description" in body
            and body["description"] is not None
            and (not isinstance(body["description"], str))
        ):
            raise HTTPException(422, "简介必须为文本")
        if "images" in body and (
            not isinstance(body["images"], list)
            or any((not isinstance(value, str) for value in body["images"]))
        ):
            raise HTTPException(422, "图片必须为字符串列表")
        if any(
            (
                key in body and (not isinstance(body[key], bool))
                for key in ("is_nsfw", "is_abyss")
            )
        ):
            raise HTTPException(422, "分类必须为布尔值")
        if "attachments" in body:
            try:
                body = {
                    **body,
                    "attachments": [
                        NativeAttachment.model_validate(item).model_dump(
                            exclude_none=True
                        )
                        for item in body["attachments"]
                    ],
                }
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, "原生附件描述无效") from exc
            if len(body["attachments"]) > 10:
                raise HTTPException(422, "最多10个原生附件")
        async with self.sessions() as session:
            service = WorkService(session)
            work = await service.visible(work_id, principal, owner=True)
            await service.update(work, body, principal, principal["forums"])
            await session.flush()
            result = (await service.project([work]))[0]
            await session.commit()
            return result

    async def delete_work(self, work_id: int, command: WorkCommandQo):
        """普通作品软删只隐藏并建立独立原生锁来源。"""
        principal = command.actor
        from work.work_moderation_service import WorkModerationService

        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, principal, owner=True)
            await WorkModerationService(session).soft_delete(work, principal, True)
            await session.commit()
        return {"deleted": True}

    async def view(self, work_id: int, command: WorkCommandQo):
        """在权限检查之后执行浏览去重。"""
        principal = command.actor
        async with self.sessions() as session:
            service = WorkService(session)
            work = await service.visible(work_id, principal)
            return await service.view(work, principal, RedisManager.get_client())

    async def tasks(self, work_id: int, command: WorkCommandQo):
        """作者查询异步任务状态，不返回内部或敏感参数。"""
        principal = command.actor
        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, principal)
            query = select(WorkTask).where(
                WorkTask.work_id == work_id, WorkTask.kind != "projection"
            )
            if work.author_id != principal["id"] and not principal["is_admin"]:
                query = query.where(
                    WorkTask.payload["actor_id"].as_string() == principal["id"]
                )
            rows = (
                (await session.execute(query.order_by(WorkTask.id.desc()).limit(100)))
                .scalars()
                .all()
            )
            return {
                "items": [
                    {
                        "id": str(row.id),
                        "kind": row.kind,
                        "status": row.status,
                        "attempts": row.attempts,
                        "error": row.error,
                        "available_at": row.available_at,
                    }
                    for row in rows
                ]
            }
