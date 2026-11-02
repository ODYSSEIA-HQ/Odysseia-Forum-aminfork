"""动态通知 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Query
from api.v2.dependencies import current_member
from work.work_notification_service import WorkNotificationService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.get("/me/notifications")
async def notifications(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """撤回公告在原通知显示撤回状态，隐藏作品不出现在通知总数中。"""
    return await WorkNotificationService(get_session_factory()).notifications(
        offset=offset,
        limit=limit,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.post("/me/notifications/{notification_id}/read")
async def read_notification(notification_id: int, principal=Depends(current_member)):
    """只允许当前用户标记自己的通知。"""
    return await WorkNotificationService(get_session_factory()).read_notification(
        notification_id=notification_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
