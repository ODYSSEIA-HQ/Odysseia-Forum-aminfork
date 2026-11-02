"""作品搜索 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Query
from api.v2.dependencies import current_member
from api.v2.schemas.work_search_request import WorkSearchRequest
from work.work_search_api_service import WorkSearchApiService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.get("/works")
@router.get("/discovery")
async def list_works(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    q: str | None = None,
    is_nsfw: bool | None = None,
    is_abyss: bool | None = None,
    author_id: str | None = None,
    tag_id: int | None = None,
    principal=Depends(current_member),
):
    """所有列表统一过滤隐藏分区后分页。"""
    return await WorkSearchApiService(get_session_factory()).list_works(
        offset=offset,
        limit=limit,
        q=q,
        is_nsfw=is_nsfw,
        is_abyss=is_abyss,
        author_id=author_id,
        tag_id=tag_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.post("/search")
async def advanced_search(body: WorkSearchRequest, principal=Depends(current_member)):
    """高级搜索使用 V1 相同请求语法和算法，返回作品分页。"""
    return await WorkSearchApiService(get_session_factory()).advanced_search(
        query=body,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
