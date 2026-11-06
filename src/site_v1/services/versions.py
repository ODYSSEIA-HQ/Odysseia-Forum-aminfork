"""协议 3.2 作品版本：序列化、最新版本批量读取、发版本、种子数据。

给作品领域用：
    latest = await latest_versions(session, [w.id for w in works], ctx)   # {work_id: Version dto}
    work_dto["latestVersion"] = latest.get(w.id)
"""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from models.attachment import Attachment
from models.work import Work
from models.work_statistics import WorkStatistics
from models.work_version import WorkVersion, WorkVersionFile
from shared.time_utils import utc_now
from site_v1.context import SiteContext
from site_v1.dto import id_str, iso_utc
from site_v1.errors import invalid
from site_v1.services.attachments import (
    attachment_dto,
    storage_key,
    usable_work_attachments,
)
from site_v1.services.work_access import parse_id, work_rating

NAME_MAX = 40
CHANGELOG_MAX = 5000
LICENSE_MAX = 200
DEPENDENCIES_MAX = 20
DEPENDENCY_NAME_MAX = 60


def download_path(work_id: int, version_id: int, file_id: int) -> str:
    return f"/api/v1/works/{work_id}/versions/{version_id}/files/{file_id}/download"


def version_dto(v: WorkVersion, files: list[Attachment], ctx: SiteContext) -> dict:
    """协议 Version。文件的 url 是下载接口地址（要登录、记下载数、302 到签名地址）。"""
    return {
        "id": id_str(v.id),
        "workId": id_str(v.work_id),
        "name": v.name,
        "changelog": v.changelog or "",
        "files": [
            attachment_dto(
                a, ctx, url=download_path(v.work_id, v.id, a.id), thumb=False
            )
            for a in files
        ],
        "dependencies": [
            {"workId": d.get("workId"), "url": d.get("url"), "name": d.get("name", "")}
            for d in (v.dependencies or [])
            if isinstance(d, dict)
        ],
        "license": v.license or "",
        "createdAt": iso_utc(v.created_at),
    }


async def load_version_files(session, version_ids) -> dict[int, list[Attachment]]:
    ids = list(set(version_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(WorkVersionFile.version_id, Attachment)
            .join(Attachment, Attachment.id == WorkVersionFile.attachment_id)
            .where(WorkVersionFile.version_id.in_(ids), Attachment.deleted_at.is_(None))
            .order_by(WorkVersionFile.version_id, WorkVersionFile.position)
        )
    ).all()
    result: dict[int, list[Attachment]] = {i: [] for i in ids}
    for version_id, att in rows:
        result[version_id].append(att)
    return result


async def serialize_versions(session, versions, ctx: SiteContext) -> list[dict]:
    files = await load_version_files(session, [v.id for v in versions])
    return [version_dto(v, files.get(v.id, []), ctx) for v in versions]


async def latest_versions(session, work_ids, ctx: SiteContext) -> dict[int, dict]:
    """每个作品的最新版本（created_at 最大，同时间取 id 大的）；没有版本的不在结果里。"""
    ids = list({i for i in work_ids if i})
    if not ids:
        return {}
    ranked = (
        select(
            WorkVersion.id,
            func.row_number()
            .over(
                partition_by=WorkVersion.work_id,
                order_by=(WorkVersion.created_at.desc(), WorkVersion.id.desc()),
            )
            .label("rn"),
        )
        .where(WorkVersion.work_id.in_(ids))
        .subquery()
    )
    versions = (
        (
            await session.execute(
                select(WorkVersion)
                .join(ranked, ranked.c.id == WorkVersion.id)
                .where(ranked.c.rn == 1)
            )
        )
        .scalars()
        .all()
    )
    dtos = await serialize_versions(session, versions, ctx)
    return {v.work_id: d for v, d in zip(versions, dtos)}


async def latest_version_row(session, work_id: int) -> WorkVersion | None:
    return (
        await session.execute(
            select(WorkVersion)
            .where(WorkVersion.work_id == work_id)
            .order_by(WorkVersion.created_at.desc(), WorkVersion.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ---------------- 发版本 ----------------


def _text(value) -> str | None:
    """去掉控制字符（保留换行、制表符）；不是字符串返回 None。"""
    if not isinstance(value, str):
        return None
    return "".join(ch for ch in value if ch >= " " or ch in "\t\n\r")


def parse_dependencies(raw) -> list[dict] | None:
    if not isinstance(raw, list) or len(raw) > DEPENDENCIES_MAX:
        return None
    result = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = (_text(item.get("name")) or "").strip()
        if not name or len(name) > DEPENDENCY_NAME_MAX:
            return None
        work_id = item.get("workId")
        if work_id is not None and parse_id(work_id) is None:
            return None
        url = item.get("url")
        if url is not None and (
            not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
            or len(url) > 2000
        ):
            return None
        result.append(
            {
                "workId": str(work_id) if work_id is not None else None,
                "url": url,
                "name": name,
            }
        )
    return result


async def create_version(
    session, work: Work, body: dict, creator_id: str
) -> WorkVersion:
    """校验并写入一个版本（不提交）。没传 fileIds / dependencies / license 时沿用上一版。"""
    fields: dict[str, str] = {}
    name = _text(body.get("name"))
    name = name.strip() if name is not None else ""
    if not name or len(name) > NAME_MAX:
        fields["name"] = f"版本名 1-{NAME_MAX} 个字"
    elif (
        await session.execute(
            select(WorkVersion.id).where(
                WorkVersion.work_id == work.id, WorkVersion.name == name
            )
        )
    ).first():
        fields["name"] = "已经有同名的版本了"
    changelog = (
        _text(body.get("changelog", "")) if body.get("changelog") is not None else ""
    )
    if changelog is None or len(changelog) > CHANGELOG_MAX:
        fields["changelog"] = f"更新说明最多 {CHANGELOG_MAX} 字"
    license_text = (
        _text(body.get("license", "")) if body.get("license") is not None else ""
    )
    if license_text is None or len(license_text.strip()) > LICENSE_MAX:
        fields["license"] = f"授权说明最多 {LICENSE_MAX} 字"

    previous = await latest_version_row(session, work.id)
    files: list[Attachment] = []
    if body.get("fileIds") is not None:
        found = await usable_work_attachments(
            session, body["fileIds"], work_id=work.id, purposes=("version_file",)
        )
        if found is None:
            fields["fileIds"] = "文件不存在、还没处理完，或者不是这个作品的版本文件"
        else:
            files = found
    elif previous is not None:
        files = (await load_version_files(session, [previous.id])).get(previous.id, [])

    if body.get("dependencies") is not None:
        dependencies = parse_dependencies(body["dependencies"])
        if dependencies is None:
            fields["dependencies"] = (
                f"依赖最多 {DEPENDENCIES_MAX} 个，每个要有名字（{DEPENDENCY_NAME_MAX} 字以内），"
                "地址要是 http(s)"
            )
    else:
        dependencies = list(previous.dependencies or []) if previous else []
    if fields:
        raise invalid(fields)

    version = WorkVersion(
        work_id=work.id,
        name=name,
        changelog=changelog,
        license=(license_text or "").strip() or (previous.license if previous else ""),
        dependencies=dependencies,
        created_by=creator_id,
        created_at=utc_now(),
    )
    session.add(version)
    await session.flush()
    for position, att in enumerate(files):
        session.add(
            WorkVersionFile(
                version_id=version.id, attachment_id=att.id, position=position
            )
        )
    work.updated_at = utc_now()
    await session.flush()
    return version


async def count_download(session, work_id: int) -> None:
    """work_statistics.download_count +1（没有统计行就建一行）。"""
    statement = insert(WorkStatistics).values(
        work_id=work_id,
        display_count=0,
        view_count=0,
        collection_count=0,
        reaction_count=0,
        reply_count=0,
        download_count=1,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[WorkStatistics.work_id],
            set_={"download_count": WorkStatistics.download_count + 1},
        )
    )


# ---------------- 种子 ----------------


async def seed_version_with_file(
    session_factory,
    *,
    work_id: int,
    owner_id: str,
    name: str = "v1.0",
    filename: str = "character-card.json",
    data: bytes | None = None,
    mime: str = "application/json",
    changelog: str = "首个版本",
    license: str = "CC BY-NC 4.0",
) -> tuple[int, int]:
    """给种子作品建一个版本和一个真实文件（文件写进当前存储：本地是 SITE_STORAGE_LOCAL_DIR）。

    幂等：同名版本已存在就直接返回。返回 (version_id, attachment_id)。
    """
    from site_v1.storage import get_storage

    async with session_factory() as session:
        existing = (
            await session.execute(
                select(WorkVersion).where(
                    WorkVersion.work_id == work_id, WorkVersion.name == name
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            link = (
                await session.execute(
                    select(WorkVersionFile.attachment_id)
                    .where(WorkVersionFile.version_id == existing.id)
                    .order_by(WorkVersionFile.position)
                    .limit(1)
                )
            ).scalar_one_or_none()
            return existing.id, link
        work = await session.get(Work, work_id)
        if work is None:
            raise ValueError(f"作品 {work_id} 不存在")
        if data is None:
            data = (
                '{"spec":"chara_card_v2","data":{"name":"种子角色","description":"种子数据"}}'
            ).encode()
        key = storage_key(filename)
        await get_storage().put(key, data, mime)
        att = Attachment(
            owner_id=owner_id,
            purpose="version_file",
            work_id=work_id,
            kind="file",
            filename=filename,
            mime=mime,
            size=len(data),
            storage_key=key,
            state="ready",
            rating=work_rating(work),
            review_state="auto",
            completed_at=utc_now(),
        )
        session.add(att)
        await session.flush()
        version = WorkVersion(
            work_id=work_id,
            name=name,
            changelog=changelog,
            license=license,
            dependencies=[],
            created_by=owner_id,
            created_at=utc_now(),
        )
        session.add(version)
        await session.flush()
        session.add(
            WorkVersionFile(version_id=version.id, attachment_id=att.id, position=0)
        )
        await session.execute(
            update(Work).where(Work.id == work_id).values(updated_at=utc_now())
        )
        ids = (version.id, att.id)
        await session.commit()
        return ids
