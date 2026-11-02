"""作品标签治理领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from work.work_tag_repository import WorkTagRepository


class WorkTagApiService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def work_tag(self, work_id: int, tag_id: int, command: WorkCommandQo):
        """作者直接挂标仍经过既有标签冲突、分类与轮次治理。"""
        principal = command.actor
        from tag.custom_tag_service import CustomTagService
        from shared.tag_error import TagError

        async with self.sessions() as session:
            service = CustomTagService(session, principal.get("config", {}))
            service.work_principal = principal
            actor = int(principal["discord_user_id"])
            payload = {"target_type": "work", "target_id": work_id}
            try:
                snapshot = await service.read(actor, payload)
                current = {
                    item["id"] for item in snapshot["tags"] if not item.get("readonly")
                }
                if command.operation == "bind":
                    current.add(str(tag_id))
                else:
                    current.discard(str(tag_id))
                await service.replace(
                    actor,
                    {
                        **payload,
                        "tag_ids": list(current),
                        "version": snapshot["version"],
                    },
                )
                await session.commit()
            except TagError as exc:
                raise HTTPException(exc.status, exc.detail) from exc
        return {"tag_id": str(tag_id), "bound": command.operation == "bind"}

    async def tags(self, q: str, command: WorkCommandQo):
        """供作品挂标选择，过滤不可见的深渊标签。"""
        principal = command.actor
        async with self.sessions() as session:
            rows = await WorkTagRepository(session).candidates(
                q, principal["can_view_abyss"]
            )
            return {"items": [{"id": str(row.id), "name": row.name} for row in rows]}

    async def work_tag_governance(
        self,
        work_id: int,
        proposal_id: int | None,
        binding_id: int | None,
        review_queue: bool,
        command: WorkCommandQo,
    ):
        """成员申请七天审核或投票，禁言不限制标签治理。"""
        principal = command.actor
        body = dict(command.values)
        from tag.custom_tag_service import CustomTagService
        from shared.tag_error import TagError
        from dto.events.tag_command import TagCommand

        action = command.operation
        if proposal_id is not None:
            action = "review"
        if binding_id is not None:
            action = "vote"
        payload = {
            "target_type": "work",
            "target_id": work_id,
            "review_queue": review_queue,
            **body,
        }
        payload["target_type"] = "work"
        payload["target_id"] = work_id
        if proposal_id is not None:
            payload["proposal_id"] = proposal_id
        if binding_id is not None:
            payload["binding_id"] = binding_id
        async with self.sessions() as session:
            service = CustomTagService(session, principal.get("config", {}))
            service.work_principal = principal
            try:
                result = await service.dispatch(
                    TagCommand(action, int(principal["discord_user_id"]), payload)
                )
                await session.commit()
            except TagError as exc:
                raise HTTPException(exc.status, exc.detail) from exc
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(422, "标签参数不完整或无效") from exc
        return {"items": result} if isinstance(result, list) else result
