"""默认收藏 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import Request, APIRouter, Depends, Query
from api.v2.dependencies import current_member
from work.work_collection_service import WorkCollectionService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.post("/works/{work_id}/collection")
@router.delete("/works/{work_id}/collection")
async def collection(work_id: int, request: Request, principal=Depends(current_member)):
    """用默认书单承载收藏，不新建作品收藏表。"""
    return await WorkCollectionService(get_session_factory()).collection(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values={},
            operation="collect" if request.method == "POST" else "uncollect",
        ),
    )


@router.get("/me/collection")
async def my_collection(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """收藏分页复用作品可见性和总数规则。"""
    return await WorkCollectionService(get_session_factory()).my_collection(
        offset=offset,
        limit=limit,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
