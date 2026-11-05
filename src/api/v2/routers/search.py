"""作品搜索 HTTP 协议适配。"""

import asyncio

from shared.enum.search_timeout import SearchTimeout
from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Query, HTTPException
from api.v2.dependencies import current_member
from api.v2.schemas.work_search_request import WorkSearchRequest
from api.v2.search_rate_limit import similar_work_rate_limit
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


@router.get("/search/suggestions")
async def search_suggestions(
    keyword: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(3, ge=1, le=10),
    apply_preferences: bool = True,
    principal=Depends(current_member),
):
    """搜索联想返回逻辑作品、统一作者、当前标签和可见书单。"""
    from work.work_discovery_service import WorkDiscoveryService

    return await _discovery_result(
        WorkDiscoveryService(get_session_factory()).suggestions(
            keyword,
            limit,
            apply_preferences,
            WorkCommandQo(
                actor=WorkActorDTO.from_principal(principal), values={}, operation=None
            ),
        ),
        SearchTimeout.SUGGESTION.value,
    )


@router.get("/works/{work_id}/similar", dependencies=[Depends(similar_work_rate_limit)])
async def similar_works(
    work_id: int,
    limit: int = Query(5, ge=1, le=20),
    apply_preferences: bool = True,
    principal=Depends(current_member),
):
    """相似推荐以逻辑作品为源，权限过滤在每层分页之前生效。"""
    from work.work_discovery_service import WorkDiscoveryService

    return await _discovery_result(
        WorkDiscoveryService(get_session_factory()).similar(
            work_id,
            limit,
            apply_preferences,
            WorkCommandQo(
                actor=WorkActorDTO.from_principal(principal), values={}, operation=None
            ),
        ),
        SearchTimeout.SIMILAR_THREADS.value,
    )


async def _discovery_result(operation, timeout):
    """沿用 V1 查询时限，超时返回可重试错误而非部分结果。"""
    try:
        return await asyncio.wait_for(operation, timeout=timeout)
    except TimeoutError as exc:
        raise HTTPException(504, "查询超时，请稍后重试") from exc
