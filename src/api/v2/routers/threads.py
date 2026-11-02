"""原生讨论区 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Body
from api.v2.dependencies import current_member
from work.work_thread_service import WorkThreadService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.get("/works/{work_id}/threads")
async def threads(
    work_id: int, include_deleted: bool = False, principal=Depends(current_member)
):
    """列出作品所有有效来源和子帖。"""
    return await WorkThreadService(get_session_factory()).threads(
        work_id=work_id,
        include_deleted=include_deleted,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.post("/works/{work_id}/threads", status_code=202)
async def create_child(
    work_id: int, body: dict = Body(...), principal=Depends(current_member)
):
    """子帖只能由 Forum 创建，主帖锁归档时拒绝新建。"""
    return await WorkThreadService(get_session_factory()).create_child(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        ),
    )


@router.patch("/works/{work_id}/threads/{thread_id}")
async def edit_child(
    work_id: int,
    thread_id: int,
    body: dict = Body(...),
    principal=Depends(current_member),
):
    """作者编辑自己子帖；作品作者只能独立加锁归档他人子帖。"""
    return await WorkThreadService(get_session_factory()).edit_child(
        work_id=work_id,
        thread_id=thread_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        ),
    )


@router.delete("/works/{work_id}/threads/{thread_id}")
async def delete_child(work_id: int, thread_id: int, principal=Depends(current_member)):
    """主帖删除软删作品；子帖只允许自己的作者或管理删除。"""
    return await WorkThreadService(get_session_factory()).delete_child(
        work_id=work_id,
        thread_id=thread_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.post("/works/{work_id}/threads/{thread_id}/restore")
async def restore_child(
    work_id: int, thread_id: int, principal=Depends(current_member)
):
    """仅管理恢复普通子帖，保留其他人的独立锁。"""
    return await WorkThreadService(get_session_factory()).restore_child(
        work_id=work_id,
        thread_id=thread_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.get("/works/{work_id}/threads/{thread_id}")
async def read_native_thread(
    work_id: int, thread_id: int, principal=Depends(current_member)
):
    """校验作品可见性后从 Fluxer 读取原生首条消息。"""
    return await WorkThreadService(get_session_factory()).read_native_thread(
        work_id=work_id,
        thread_id=thread_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
