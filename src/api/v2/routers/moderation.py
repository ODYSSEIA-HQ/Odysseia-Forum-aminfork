"""作品管理 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Body, Query
from api.v2.dependencies import current_member
from work.work_moderation_api_service import WorkModerationApiService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.post("/works/{work_id}/restore")
async def restore_work(work_id: int, principal=Depends(current_member)):
    """管理恢复普通隐藏作品，权威源主帖删除禁止恢复。"""
    return await WorkModerationApiService(get_session_factory()).restore_work(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.get("/works/{work_id}/mutes")
async def list_mutes(work_id: int, principal=Depends(current_member)):
    """作品作者和管理可查看各独立处罚来源及到期时间。"""
    return await WorkModerationApiService(get_session_factory()).list_mutes(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.post("/works/{work_id}/mutes", status_code=202)
async def mute_user(
    work_id: int, body: dict = Body(...), principal=Depends(current_member)
):
    """作者或管理建立作品禁言并异步覆盖全部原生帖子。"""
    return await WorkModerationApiService(get_session_factory()).mute_user(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        ),
    )


@router.delete("/works/{work_id}/mutes/{mute_id}", status_code=202)
async def unmute_user(work_id: int, mute_id: int, principal=Depends(current_member)):
    """只释放当前操作者自己的作品处罚来源。"""
    return await WorkModerationApiService(get_session_factory()).unmute_user(
        work_id=work_id,
        mute_id=mute_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.get("/works/{work_id}/modlog")
async def moderation_log(
    work_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """作者或管理查询作品操作记录，保留实际操作者与原因。"""
    return await WorkModerationApiService(get_session_factory()).moderation_log(
        work_id=work_id,
        offset=offset,
        limit=limit,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
