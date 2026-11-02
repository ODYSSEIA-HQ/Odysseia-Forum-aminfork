"""作品标签治理 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import Request, APIRouter, Depends, Body
from api.v2.dependencies import current_member
from work.work_tag_api_service import WorkTagApiService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.put("/works/{work_id}/tags/{tag_id}")
@router.delete("/works/{work_id}/tags/{tag_id}")
async def work_tag(
    work_id: int, tag_id: int, request: Request, principal=Depends(current_member)
):
    """作者直接挂标仍经过既有标签冲突、分类与轮次治理。"""
    return await WorkTagApiService(get_session_factory()).work_tag(
        work_id=work_id,
        tag_id=tag_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values={},
            operation="bind" if request.method == "PUT" else "unbind",
        ),
    )


@router.get("/tags")
async def tags(q: str = "", principal=Depends(current_member)):
    """供作品挂标选择，过滤不可见的深渊标签。"""
    return await WorkTagApiService(get_session_factory()).tags(
        q=q,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.get("/works/{work_id}/tags")
@router.get("/works/{work_id}/tag-proposals")
@router.post("/works/{work_id}/tag-proposals")
@router.patch("/works/{work_id}/tag-proposals/{proposal_id}")
@router.put("/works/{work_id}/tag-bindings/{binding_id}/vote")
async def work_tag_governance(
    work_id: int,
    request: Request,
    proposal_id: int | None = None,
    binding_id: int | None = None,
    review_queue: bool = False,
    body: dict = Body(default={}),
    principal=Depends(current_member),
):
    """成员申请七天审核或投票，禁言不限制标签治理。"""
    operation = (
        "read"
        if request.url.path.endswith("/tags")
        else "proposals"
        if request.method == "GET"
        else "propose"
    )
    if proposal_id is not None:
        operation = "review"
    if binding_id is not None:
        operation = "vote"
    return await WorkTagApiService(get_session_factory()).work_tag_governance(
        work_id=work_id,
        proposal_id=proposal_id,
        binding_id=binding_id,
        review_queue=review_queue,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values=body,
            operation=operation,
        ),
    )
