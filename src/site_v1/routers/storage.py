"""本地存储的签名端点（不属于协议，不在 /api/v1 下，所以不走会话和 CSRF）。

只在 SITE_STORAGE_BACKEND=local 时有用；用 S3/R2 时浏览器直连对象存储，这两个端点一律 404。
- PUT /api/storage/upload/{token}：直传。Content-Type 要和申请时一致，字节数要和申请时一样。
- GET /api/storage/files/{token}：读取（图片地址、下载 302 的目标）。
"""

import time
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from site_v1.storage import get_storage
from site_v1.storage.local import STORAGE_PREFIX, LocalStorage

router = APIRouter(prefix=STORAGE_PREFIX, include_in_schema=False)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _local() -> LocalStorage | None:
    storage = get_storage()
    return storage if isinstance(storage, LocalStorage) else None


@router.put("/upload/{token}")
async def put_object(token: str, request: Request):
    storage = _local()
    payload = storage.verify(token, "put") if storage else None
    if payload is None:
        return _error(403, "上传地址无效或已过期")
    declared = int(payload.get("s", 0))
    content_type = (
        (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    )
    if content_type != payload.get("m"):
        return _error(400, "Content-Type 和申请时不一致")
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) != declared:
        return _error(400, "文件大小和申请时不一致")

    received = 0

    async def chunks():
        nonlocal received
        async for chunk in request.stream():
            received += len(chunk)
            if received > declared:
                raise ValueError("文件比申请时大")
            yield chunk

    try:
        await storage.write_chunks(payload["k"], chunks())
    except ValueError:
        await storage.delete(payload["k"])
        return _error(400, "文件大小和申请时不一致")
    if received != declared:
        await storage.delete(payload["k"])
        return _error(400, "文件大小和申请时不一致")
    return Response(status_code=200)


@router.get("/files/{token}")
async def get_object(token: str):
    storage = _local()
    payload = storage.verify(token, "get") if storage else None
    if payload is None:
        return _error(403, "地址无效或已过期")
    path = storage.path_of(payload["k"])
    if not path.is_file():
        return _error(404, "文件不存在")
    headers = {
        "Cache-Control": f"private, max-age={max(0, payload['e'] - int(time.time()))}",
        "X-Content-Type-Options": "nosniff",
        # 用户上传的内容和站点同源时也不能执行脚本
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'",
    }
    name = payload.get("n")
    if name:
        disposition = "attachment" if payload.get("d") else "inline"
        headers["Content-Disposition"] = (
            f"{disposition}; filename*=UTF-8''{quote(name)}"
        )
    return FileResponse(
        path, media_type=payload.get("m") or "application/octet-stream", headers=headers
    )
