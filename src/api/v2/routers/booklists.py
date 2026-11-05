"""作品书单 HTTP 协议适配。"""

from typing import Literal
from shared.request_id import RequestId

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


@router.get("/booklists/page")
async def booklists_page(
    scope: Literal["public", "mine", "collected"] = "public",
    include_tag_ids: list[RequestId] | None = Query(None, max_length=100),
    exclude_tag_ids: list[RequestId] | None = Query(None, max_length=100),
    tag_logic: Literal["and", "or"] = "and",
    owner_id: str | None = None,
    mark_work_id: int | None = Query(None, gt=0, le=9223372036854775807),
    keywords: str | None = Query(None, max_length=600),
    sort_method: int = Query(4, ge=1, le=6),
    sort_order: Literal["asc", "desc"] = "desc",
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """按访问范围与标签条件返回作品书单分页。"""
    return await WorkBooklistService(get_session_factory()).page(
        scope,
        owner_id,
        keywords,
        sort_method,
        sort_order,
        offset,
        limit,
        WorkCommandQo(actor=WorkActorDTO.from_principal(principal)),
        mark_work_id=mark_work_id,
        include_tag_ids=include_tag_ids,
        exclude_tag_ids=exclude_tag_ids,
        tag_logic=tag_logic,
    )


@router.post("/booklists/items/sync")
async def sync_booklist_item(body: dict = Body(...), principal=Depends(current_member)):
    """在指定书单范围同步作品的归属与推荐信息。"""
    return await WorkBooklistService(get_session_factory()).sync_item(
        WorkCommandQo(actor=WorkActorDTO.from_principal(principal), values=body)
    )


@router.post("/booklists/{booklist_id}/items")
async def add_booklist_items(
    booklist_id: int, body: dict = Body(...), principal=Depends(current_member)
):
    """批量添加作品条目并复用统一身份与字段校验。"""
    return await WorkBooklistService(get_session_factory()).add_items(
        booklist_id,
        WorkCommandQo(actor=WorkActorDTO.from_principal(principal), values=body),
    )


@router.put("/booklists/{booklist_id}/collection")
@router.delete("/booklists/{booklist_id}/collection")
async def collect_booklist(
    booklist_id: int, request: Request, principal=Depends(current_member)
):
    """按当前用户权限更新书单收藏及公开计数。"""
    return await WorkBooklistService(get_session_factory()).collect_booklist(
        booklist_id,
        WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            operation="add" if request.method == "PUT" else "remove",
        ),
    )


@router.get("/booklists/{booklist_id}/presentation")
async def booklist_presentation(booklist_id: int, principal=Depends(current_member)):
    """返回经过作品可见性过滤的书单展示资料。"""
    return await WorkBooklistService(get_session_factory()).presentation(
        booklist_id, WorkCommandQo(actor=WorkActorDTO.from_principal(principal))
    )


@router.get("/booklists/{booklist_id}/items")
async def booklist_items(
    booklist_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    principal=Depends(current_member),
):
    """按稳定顺序分页返回书单内可见作品。"""
    return await WorkBooklistService(get_session_factory()).items(
        booklist_id,
        offset,
        limit,
        WorkCommandQo(actor=WorkActorDTO.from_principal(principal)),
    )


@router.delete("/booklists/{booklist_id}")
async def delete_booklist(booklist_id: int, principal=Depends(current_member)):
    """校验管理权限后删除普通书单及其关联。"""
    return await WorkBooklistService(get_session_factory()).delete_booklist(
        booklist_id, WorkCommandQo(actor=WorkActorDTO.from_principal(principal))
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


@router.patch("/booklists/{booklist_id}/works/{work_id}")
@router.put("/booklists/{booklist_id}/works/{work_id}")
@router.delete("/booklists/{booklist_id}/works/{work_id}")
async def booklist_item(
    booklist_id: int,
    work_id: int,
    request: Request,
    body: dict | None = Body(None),
    principal=Depends(current_member),
):
    """书单条目以作品为单位去重。"""
    return await WorkBooklistService(get_session_factory()).booklist_item(
        booklist_id=booklist_id,
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal),
            values=body or {},
            operation={"PUT": "add", "PATCH": "edit", "DELETE": "remove"}[
                request.method
            ],
        ),
    )
