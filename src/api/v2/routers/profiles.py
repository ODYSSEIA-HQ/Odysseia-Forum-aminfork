"""账户偏好 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Body
from api.v2.dependencies import current_member
from work.work_profile_service import WorkProfileService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.get("/me")
async def me(principal=Depends(current_member)):
    """公开前端需要的能力，不泄漏会话凭据。"""
    return await WorkProfileService(get_session_factory()).me(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        )
    )


@router.get("/me/preferences")
async def preferences(principal=Depends(current_member)):
    """返回用户全局成人内容偏好。"""
    return await WorkProfileService(get_session_factory()).preferences(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        )
    )


@router.patch("/me/preferences")
async def save_preferences(body: dict = Body(...), principal=Depends(current_member)):
    """偏好存统一用户表，不随服务器覆盖。"""
    return await WorkProfileService(get_session_factory()).save_preferences(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        )
    )
