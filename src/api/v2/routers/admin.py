"""管理后台 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Body, Query
from api.v2.dependencies import current_member
from work.work_admin_service import WorkAdminService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.post("/admin/users/{target_user_id}/discord-binding", status_code=202)
async def managed_rebind(
    target_user_id: str, body: dict = Body(...), principal=Depends(current_member)
):
    """只有当前 Discord 管理组可启动显式换绑和业务合并。"""
    return await WorkAdminService(get_session_factory()).managed_rebind(
        target_user_id=target_user_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        ),
    )


@router.get("/admin/tasks/{task_id}")
async def admin_task(task_id: int, principal=Depends(current_member)):
    """管理查看显式身份任务，不回传用户令牌。"""
    return await WorkAdminService(get_session_factory()).admin_task(
        task_id=task_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.get("/admin/deleted-works")
async def deleted_works(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """管理查询已隐藏作品，仍遵守全局 NSFW 与深渊访问门槛。"""
    return await WorkAdminService(get_session_factory()).deleted_works(
        offset=offset,
        limit=limit,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
