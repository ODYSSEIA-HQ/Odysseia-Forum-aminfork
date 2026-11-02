"""作品关注 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import Request, APIRouter, Depends, Query
from api.v2.dependencies import current_member
from work.work_follow_service import WorkFollowService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.put("/works/{work_id}/follow")
@router.delete("/works/{work_id}/follow")
async def follow(work_id: int, request: Request, principal=Depends(current_member)):
    """关注只影响未来公告，不补发历史通知。"""
    return await WorkFollowService(get_session_factory()).follow(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values={},
            operation="follow" if request.method == "PUT" else "unfollow",
        ),
    )


@router.get("/me/follows")
async def follows(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """关注列表采用统一作品可见性。"""
    return await WorkFollowService(get_session_factory()).follows(
        offset=offset,
        limit=limit,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
