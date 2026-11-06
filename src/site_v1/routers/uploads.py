"""协议 3.3：POST /uploads、POST /uploads/{id}/complete、GET /uploads/{id}。

实现在 services/attachments.py；存储适配器在 site_v1/storage，分级器在 site_v1/classifier.py。
"""

from fastapi import APIRouter, Body, Request

from site_v1.deps import Ctx, CurrentUser
from site_v1.services import attachments

router = APIRouter(tags=["上传"])


@router.post("/uploads", status_code=201, summary="申请上传（返回直传地址）")
async def post_upload(request: Request, viewer: CurrentUser, body: dict = Body(...)):
    return await attachments.create_upload(
        viewer, body, base=str(request.base_url).rstrip("/")
    )


@router.post("/uploads/{id}/complete", summary="通知上传完成")
async def complete_upload(id: str, viewer: CurrentUser):
    return {"state": await attachments.complete_upload(viewer, id)}


@router.get("/uploads/{id}", summary="查上传状态（附件对象）")
async def get_upload(id: str, viewer: CurrentUser, ctx: Ctx):
    return await attachments.get_upload(viewer, id, ctx)
