"""管理后台领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from models.user import User
from models.work_task import WorkTask
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkAdminService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def managed_rebind(self, target_user_id: str, command: WorkCommandQo):
        """只有当前 Discord 管理组可启动显式换绑和业务合并。"""
        principal = command.actor
        body = dict(command.values)
        if not principal["is_admin"]:
            raise HTTPException(403, "需要管理组权限")
        discord_id = body.get("discord_user_id")
        reason = body.get("reason")
        if (
            not isinstance(discord_id, str)
            or not discord_id.isdecimal()
            or discord_id.startswith("0")
            or (not isinstance(reason, str))
            or (not reason.strip())
            or set(body) - {"discord_user_id", "reason", "profile"}
        ):
            raise HTTPException(422, "需要 Discord ID 与操作原因")
        if len(reason) > 512:
            raise HTTPException(422, "管理原因最多512字符")
        from core.tag_access_service import TagAccessService
        from shared.tag_error import TagError

        access = TagAccessService(principal.get("config", {}))
        try:
            verified = await access.get_member(int(discord_id), access.main_guild)
        except TagError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        user = verified.get("user", {})
        if str(user.get("id")) != discord_id or not isinstance(
            user.get("username"), str
        ):
            raise HTTPException(503, "目标Discord资料暂不可用")
        required = principal.get("config", {}).get("auth", {}).get("role_ids", [])
        required = required.split(",") if isinstance(required, str) else required
        if not {str(value) for value in verified.get("roles", [])}.intersection(
            (str(value).strip() for value in required)
        ):
            raise HTTPException(403, "目标Discord用户不具备正式成员身份组")
        avatar = user.get("avatar")
        import re

        if avatar and (not re.fullmatch("[A-Za-z0-9_]{1,128}", avatar)):
            raise HTTPException(503, "目标Discord头像资料无效")
        profile = {
            "username": user["username"],
            "display_name": user.get("global_name"),
            "avatar_url": f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.png"
            if avatar
            else None,
        }
        async with self.sessions() as session:
            target = await session.get(User, target_user_id)
            if target is None or target.merged_into_id:
                raise HTTPException(404, "目标用户不存在")
            task = WorkTask(
                work_id=0,
                kind="managed_rebind",
                dedupe_key="rebind:" + __import__("uuid").uuid4().hex,
                payload={
                    "actor_id": principal["id"],
                    "user_id": target_user_id,
                    "discord_user_id": discord_id,
                    "reason": reason,
                    "profile": profile,
                },
            )
            session.add(task)
            await session.flush()
            result = {"task_id": str(task.id), "status": "pending"}
            await session.commit()
            return result

    async def admin_task(self, task_id: int, command: WorkCommandQo):
        """管理查看显式身份任务，不回传用户令牌。"""
        principal = command.actor
        if not principal["is_admin"]:
            raise HTTPException(403, "需要管理组权限")
        async with self.sessions() as session:
            task = await session.get(WorkTask, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            return {
                "id": str(task.id),
                "kind": task.kind,
                "status": task.status,
                "attempts": task.attempts,
                "error": task.error,
            }

    async def deleted_works(self, offset: int, limit: int, command: WorkCommandQo):
        """管理查询已隐藏作品，仍遵守全局 NSFW 与深渊访问门槛。"""
        principal = command.actor
        if not principal["is_admin"]:
            raise HTTPException(403, "需要管理组权限")
        async with self.sessions() as session:
            rows, total = await WorkRepository(session).page(
                principal, offset, limit, deleted_only=True
            )
            projected = await WorkService(session).project(rows)
            for work, data in zip(rows, projected):
                data["deletion_reason"] = work.deletion_reason
                data["can_restore"] = work.deletion_reason != "source_deleted"
            return {
                "items": projected,
                "total": total,
                "offset": offset,
                "limit": limit,
            }
