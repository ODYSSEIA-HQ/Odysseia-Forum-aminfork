"""作品对象里的附件（封面、图库、最新版本文件）和最新版本（Version）。

只读 attachment / work_version / work_version_file 表；上传、存储、签名地址归上传领域。
地址生成留了一个钩子 set_url_resolver：上传领域接入对象存储后把它换成自己的实现
（全年龄图公开地址，R18/R18G 图绑定账号的短期签名地址）。默认实现：
- 环境变量 SITE_MEDIA_BASE 有值时用 {SITE_MEDIA_BASE}/{storage_key}；
- 否则 /api/v1/media/{storage_key}（占位，实际由上传领域定）。

有效模式不允许的分级（协议 3.3）：url、thumbUrl 为 null，placeholder=true。
"""

import os
from typing import Callable, Iterable

from sqlalchemy import select

from models.attachment import Attachment
from models.work_version import WorkVersion, WorkVersionFile
from site_v1.dto import iso_utc


def _default_url(att: Attachment, viewer_id: str | None) -> str:
    base = os.environ.get("SITE_MEDIA_BASE", "").rstrip("/")
    if base:
        return f"{base}/{att.storage_key}"
    return f"/api/v1/media/{att.storage_key}"


_url_resolver: Callable[[Attachment, str | None], str] = _default_url


def set_url_resolver(fn: Callable[[Attachment, str | None], str] | None) -> None:
    """上传领域换成真实的地址/签名实现；传 None 恢复默认。"""
    global _url_resolver
    _url_resolver = fn or _default_url


def attachment_dto(att: Attachment, ctx) -> dict:
    """协议 Attachment；ctx 是 SiteContext（按有效模式决定给不给地址）。"""
    rating = att.rating or "general"
    visible = rating in ctx.visible_ratings()
    url = (
        _url_resolver(att, ctx.viewer.user_id if ctx.viewer else None)
        if visible
        else None
    )
    state = (
        att.state if att.state in ("processing", "ready", "rejected") else "processing"
    )
    body = {
        "id": str(att.id),
        "kind": att.kind if att.kind in ("image", "file") else "file",
        "mime": att.mime,
        "size": int(att.size or 0),
        "name": att.filename,
        "url": url,
        "placeholder": not visible,
        "thumbUrl": url if att.kind == "image" else None,
        "rating": rating,
        "reviewState": att.review_state or "auto",
        "state": state,
        "rejectReason": att.reject_reason,
    }
    if att.width is not None:
        body["width"] = att.width
    if att.height is not None:
        body["height"] = att.height
    return body


async def load_attachments(session, ids: Iterable[int]) -> dict[int, Attachment]:
    """按 ID 批量取附件（已删除的、还在 uploading 的不算）。"""
    wanted = {int(i) for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(Attachment).where(
                Attachment.id.in_(wanted),
                Attachment.deleted_at.is_(None),
                Attachment.state != "uploading",
            )
        )
    ).scalars()
    return {a.id: a for a in rows}


async def latest_versions(session, work_ids: Iterable[int]):
    """每个作品的最新版本和文件：{work_id: (WorkVersion, [Attachment])}。"""
    ids = list({int(i) for i in work_ids})
    if not ids:
        return {}
    versions = (
        (
            await session.execute(
                select(WorkVersion)
                .where(WorkVersion.work_id.in_(ids))
                .order_by(
                    WorkVersion.work_id,
                    WorkVersion.created_at.desc(),
                    WorkVersion.id.desc(),
                )
                .distinct(WorkVersion.work_id)
            )
        )
        .scalars()
        .all()
    )
    if not versions:
        return {}
    files: dict[int, list[Attachment]] = {v.id: [] for v in versions}
    rows = await session.execute(
        select(WorkVersionFile.version_id, Attachment)
        .join(Attachment, Attachment.id == WorkVersionFile.attachment_id)
        .where(
            WorkVersionFile.version_id.in_(list(files)),
            Attachment.deleted_at.is_(None),
            Attachment.state != "uploading",
        )
        .order_by(WorkVersionFile.version_id, WorkVersionFile.position)
    )
    for version_id, att in rows:
        files[version_id].append(att)
    return {v.work_id: (v, files[v.id]) for v in versions}


def version_dto(version: WorkVersion, files: list[Attachment], ctx) -> dict:
    """协议 Version。"""
    return {
        "id": str(version.id),
        "workId": str(version.work_id),
        "name": version.name,
        "changelog": version.changelog or "",
        "files": [attachment_dto(a, ctx) for a in files],
        "dependencies": [
            {
                "workId": str(d["workId"]) if d.get("workId") is not None else None,
                "url": d.get("url"),
                "name": d.get("name") or "",
            }
            for d in (version.dependencies or [])
            if isinstance(d, dict)
        ],
        "license": version.license or "",
        "createdAt": iso_utc(version.created_at),
    }
