"""协议 3.2 版本：GET/POST /works/{id}/versions、
GET /works/{id}/versions/{vid}/files/{fileId}/download（302 签名地址，work_statistics.download_count +1）。

作品可见性直接读 Work 表（services/work_access.py），不依赖作品服务。
"""

from datetime import datetime

from fastapi import APIRouter, Body, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select, tuple_

from models.attachment import Attachment
from models.work_version import WorkVersion, WorkVersionFile
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.errors import invalid, not_found
from site_v1.paging import PageRequest, encode_cursor, pagination
from site_v1.services import versions as svc
from site_v1.services.work_access import (
    load_manageable_work,
    load_visible_work,
    parse_id,
)
from site_v1.storage import get_storage

router = APIRouter(tags=["作品"])


@router.get("/works/{id}/versions", summary="版本列表（新的在前）")
async def list_versions(id: str, ctx: Ctx, page: PageRequest = Depends(pagination)):
    async with get_session_factory()() as session:
        work = await load_visible_work(session, id, ctx)
        q = select(WorkVersion).where(WorkVersion.work_id == work.id)
        if page.after is not None:
            try:
                after_at, after_id = page.after
                key = (
                    datetime.fromisoformat(after_at.replace("Z", "+00:00")).replace(
                        tzinfo=None
                    ),
                    int(after_id),
                )
            except (TypeError, ValueError, AttributeError):
                raise invalid({"cursor": "游标无效，请从第一页重新开始"})
            q = q.where(tuple_(WorkVersion.created_at, WorkVersion.id) < key)
        rows = (
            (
                await session.execute(
                    q.order_by(
                        WorkVersion.created_at.desc(), WorkVersion.id.desc()
                    ).limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > page.limit
        rows = rows[: page.limit]
        items = await svc.serialize_versions(session, rows, ctx)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        # 时间要到微秒，iso_utc 只到毫秒，同一毫秒的版本会被跳过
        next_cursor = encode_cursor({"k": [last.created_at.isoformat(), last.id]})
    return {"items": items, "nextCursor": next_cursor}


@router.post("/works/{id}/versions", status_code=201, summary="发新版本")
async def create_version(
    id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)
):
    async with get_session_factory()() as session:
        work = await load_manageable_work(session, id, ctx, viewer, for_update=True)
        version = await svc.create_version(session, work, body, viewer.user_id)
        await session.commit()
        await session.refresh(version)
        (dto,) = await svc.serialize_versions(session, [version], ctx)
    return dto


@router.get(
    "/works/{id}/versions/{vid}/files/{fileId}/download",
    summary="下载（记一次下载，302 到签名地址）",
    status_code=302,
)
async def download(id: str, vid: str, fileId: str, viewer: CurrentUser, ctx: Ctx):
    version_id, file_id = parse_id(vid), parse_id(fileId)
    async with get_session_factory()() as session:
        work = await load_visible_work(session, id, ctx)
        if version_id is None or file_id is None:
            raise not_found()
        row = (
            await session.execute(
                select(Attachment)
                .join(WorkVersionFile, WorkVersionFile.attachment_id == Attachment.id)
                .join(WorkVersion, WorkVersion.id == WorkVersionFile.version_id)
                .where(
                    WorkVersion.id == version_id,
                    WorkVersion.work_id == work.id,
                    Attachment.id == file_id,
                    Attachment.deleted_at.is_(None),
                    Attachment.state == "ready",
                )
            )
        ).scalar_one_or_none()
        if row is None or (row.rating or "general") not in ctx.visible_ratings():
            raise not_found()
        key, mime, filename = row.storage_key, row.mime, row.filename
        await svc.count_download(session, work.id)
        await session.commit()
    url = get_storage().read_url(
        key,
        mime=mime,
        public=False,
        filename=filename,
        download=True,
        viewer_id=viewer.user_id,
    )
    return RedirectResponse(url, status_code=302)
