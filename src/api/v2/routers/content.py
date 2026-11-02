"""作品内容 HTTP 协议适配。"""

from dto.work.work_actor_dto import WorkActorDTO
from dto.work.work_command_qo import WorkCommandQo
from fastapi import APIRouter, Depends, Body
from api.v2.dependencies import current_member
from work.work_content_service import WorkContentService
from api.v2.dependencies import get_session_factory

router = APIRouter(prefix="/v2", tags=["作品 V2"])


@router.post("/works", status_code=202)
async def create_work(body: dict = Body(...), principal=Depends(current_member)):
    """先提交作品与可靠任务，再异步创建 Fluxer 原生主帖。"""
    return await WorkContentService(get_session_factory()).create_work(
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        )
    )


@router.get("/works/upload-target")
async def upload_target(
    is_nsfw: bool = False, is_abyss: bool = False, principal=Depends(current_member)
):
    """创建预上传仅返回当前成员可使用的已配置原生论坛。"""
    return await WorkContentService(get_session_factory()).upload_target(
        is_nsfw=is_nsfw,
        is_abyss=is_abyss,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.get("/works/{work_id}")
async def get_work(work_id: int, principal=Depends(current_member)):
    """返回完整作品投影。"""
    return await WorkContentService(get_session_factory()).get_work(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.patch("/works/{work_id}")
async def patch_work(
    work_id: int, body: dict = Body(...), principal=Depends(current_member)
):
    """作者编辑元数据，分类变动创建可靠平台任务。"""
    return await WorkContentService(get_session_factory()).patch_work(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values=body, operation=None
        ),
    )


@router.delete("/works/{work_id}")
async def delete_work(work_id: int, principal=Depends(current_member)):
    """普通作品软删只隐藏并建立独立原生锁来源。"""
    return await WorkContentService(get_session_factory()).delete_work(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.post("/works/{work_id}/view")
async def view(work_id: int, principal=Depends(current_member)):
    """在权限检查之后执行浏览去重。"""
    return await WorkContentService(get_session_factory()).view(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )


@router.get("/works/{work_id}/tasks")
async def tasks(work_id: int, principal=Depends(current_member)):
    """作者查询异步任务状态，不返回内部或敏感参数。"""
    return await WorkContentService(get_session_factory()).tasks(
        work_id=work_id,
        command=WorkCommandQo(
            actor=WorkActorDTO.from_principal(principal), values={}, operation=None
        ),
    )
