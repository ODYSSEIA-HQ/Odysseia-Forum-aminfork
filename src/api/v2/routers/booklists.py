"""作品书单 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import Request, APIRouter, Depends, Body, Query
from api.v2.dependencies import current_member
from work.work_booklist_service import WorkBooklistService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.get("/booklists")
async def booklists(principal=Depends(current_member)):
    """返回当前用户的普通及默认书单。"""
    return await WorkBooklistService(get_session_factory()).booklists(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        )
    )


@router.post("/booklists")
async def create_booklist(body: dict = Body(...), principal=Depends(current_member)):
    """创建作品书单，所有者来自认证主体。"""
    return await WorkBooklistService(get_session_factory()).create_booklist(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        )
    )


@router.get("/booklists/{booklist_id}")
async def booklist_detail(
    booklist_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """公开书单仍按访问者偏好过滤作品。"""
    return await WorkBooklistService(get_session_factory()).booklist_detail(
        booklist_id=booklist_id,
        offset=offset,
        limit=limit,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.patch("/booklists/{booklist_id}")
async def edit_booklist(
    booklist_id: int, body: dict = Body(...), principal=Depends(current_member)
):
    """更新书单元数据，禁止重定向所有权。"""
    return await WorkBooklistService(get_session_factory()).edit_booklist(
        booklist_id=booklist_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        ),
    )


@router.put("/booklists/{booklist_id}/works/{work_id}")
@router.delete("/booklists/{booklist_id}/works/{work_id}")
async def booklist_item(
    booklist_id: int, work_id: int, request: Request, principal=Depends(current_member)
):
    """书单条目以作品为单位去重。"""
    return await WorkBooklistService(get_session_factory()).booklist_item(
        booklist_id=booklist_id,
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values={},
            operation="add" if request.method == "PUT" else "remove",
        ),
    )
