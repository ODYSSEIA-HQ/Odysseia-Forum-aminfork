"""作品公告 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import Request, APIRouter, Depends, Body, Query
from api.v2.dependencies import current_member
from work.work_announcement_service import WorkAnnouncementService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.get("/works/{work_id}/announcements")
async def announcements(
    work_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """历史公告可读，不为新关注者补发通知。"""
    return await WorkAnnouncementService(get_session_factory()).announcements(
        work_id=work_id,
        offset=offset,
        limit=limit,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.post("/works/{work_id}/announcements")
async def publish_announcement(
    work_id: int, body: dict = Body(...), principal=Depends(current_member)
):
    """每次发布在同一事务中快照关注者并写入独立通知。"""
    return await WorkAnnouncementService(get_session_factory()).publish_announcement(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        ),
    )


@router.patch("/works/{work_id}/announcements/{announcement_id}")
@router.delete("/works/{work_id}/announcements/{announcement_id}")
async def edit_announcement(
    work_id: int,
    announcement_id: int,
    request: Request,
    body: dict = Body(default={}),
    principal=Depends(current_member),
):
    """编辑不通知，撤回保留原公告和原通知关联。"""
    return await WorkAnnouncementService(get_session_factory()).edit_announcement(
        work_id=work_id,
        announcement_id=announcement_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values=body,
            operation="withdraw" if request.method == "DELETE" else "edit",
        ),
    )
