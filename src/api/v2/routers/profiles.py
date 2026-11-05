"""账户偏好 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends
from api.v2.schemas.work_preferences_response import WorkPreferencesResponse
from api.v2.schemas.work_preferences_update_request import WorkPreferencesUpdateRequest
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


@router.get("/me/preferences", response_model=WorkPreferencesResponse)
async def preferences(principal=Depends(current_member)):
    """返回完整偏好及首次设置状态，不在读取时创建记录。"""
    return await WorkProfileService(get_session_factory()).preferences(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        )
    )


@router.patch("/me/preferences", response_model=WorkPreferencesResponse)
async def save_preferences(
    body: WorkPreferencesUpdateRequest, principal=Depends(current_member)
):
    """仅修改明确传入的字段，身份与作用域不能由请求覆盖。"""
    return await WorkProfileService(get_session_factory()).save_preferences(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values=body.model_dump(exclude_unset=True),
            operation=None,
        )
    )
