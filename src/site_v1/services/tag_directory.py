"""tag 浏览（草案 G）：标签池、tag 页、同义词、禁用名，以及按查看者计数。

对齐假后端 src/mock/handlers/tags.ts、governance.ts：
- 作品数只数当前查看者能看到的作品（services/works.listable_conditions）；
- 全年龄模式下作品数为 0 的 tag 不返回、不联想，按 ID 打开 404（只出现在受限作品上的 tag 不暴露存在）；
- 按同义词（或名字）打开 tag 页返回主名。
tag 本体在 site_tag（官方组 + group_id=free 的自由 tag），同义词 site_tag_alias，禁用名 site_tag_disabled。
"""

import copy
import dataclasses

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from models.site_account import SiteSetting
from models.site_nav import SiteTagAlias, SiteTagDisabled
from models.site_tag import SiteTag, SiteTagQuestion, SiteWorkTag
from models.work import Work
from models.work_statistics import WorkStatistics
from shared.time_utils import utc_now
from site_v1.dto import iso_utc
from site_v1.services import tag_rules as rules
from site_v1.services import works as svc
from site_v1.services.attachments import attachment_dto, load_attachments
from site_v1.services.work_access import parse_id

TAG_GROUPS = (
    "rating",
    "type",
    "audience",
    "form",
    "theme",
    "function",
    "warning",
    "attribute",
    "free",
)
OFFICIAL_GROUPS = tuple(g for g in TAG_GROUPS if g != rules.FREE)
# 分级、资源类型是系统按名字用的（分级判断、type 参数），不能改名、合并、禁用
STRUCTURAL_GROUPS = ("rating", "type")


def ctx_ignoring_blocked(ctx):
    """同一个查看者，但不按他屏蔽的 tag 过滤（tag 页、管理后台计数用）。"""
    viewer = ctx.viewer
    if viewer is None or not viewer.prefs.blocked_tags:
        return ctx
    clone = copy.copy(ctx)
    clone.viewer = dataclasses.replace(
        viewer, prefs=dataclasses.replace(viewer.prefs, blocked_tags=[])
    )
    return clone


async def visible_counts(
    session, ctx, tag_ids=None, *, ignore_blocked: bool = False
) -> dict[int, int]:
    """{tag_id: 当前查看者能看到的作品数}。"""
    use = ctx_ignoring_blocked(ctx) if ignore_blocked else ctx
    conds = await svc.listable_conditions(session, use)
    q = (
        select(SiteWorkTag.tag_id, func.count(func.distinct(Work.id)))
        .join(Work, Work.id == SiteWorkTag.work_id)
        .where(SiteWorkTag.removed_at.is_(None), *conds)
        .group_by(SiteWorkTag.tag_id)
    )
    if tag_ids is not None:
        ids = [int(i) for i in tag_ids]
        if not ids:
            return {}
        q = q.where(SiteWorkTag.tag_id.in_(ids))
    return {int(k): int(v) for k, v in (await session.execute(q)).all()}


async def active_tags(session) -> list[SiteTag]:
    """全部没删、没禁用的 tag：官方按组和组内顺序，自由 tag 在后。"""
    rows = (
        (await session.execute(select(SiteTag).where(SiteTag.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    order = {g: i for i, g in enumerate(TAG_GROUPS)}
    return sorted(
        rows, key=lambda t: (order.get(t.group_id, 99), t.sort_order or 0, t.id)
    )


async def aliases_of(session, tag_ids) -> dict[int, list[str]]:
    ids = [int(i) for i in tag_ids]
    out: dict[int, list[str]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = await session.execute(
        select(SiteTagAlias.tag_id, SiteTagAlias.name)
        .where(SiteTagAlias.tag_id.in_(ids))
        .order_by(SiteTagAlias.id)
    )
    for tag_id, name in rows:
        out.setdefault(int(tag_id), []).append(name)
    return out


async def all_aliases(session) -> list[tuple[int, str]]:
    return [
        (int(t), n)
        for t, n in (
            await session.execute(
                select(SiteTagAlias.tag_id, SiteTagAlias.name).order_by(SiteTagAlias.id)
            )
        ).all()
    ]


async def alias_target(session, name: str) -> SiteTag | None:
    """同义词 → 主 tag（没删的）。"""
    key = name.strip().lower()
    if not key:
        return None
    return (
        await session.execute(
            select(SiteTag)
            .join(SiteTagAlias, SiteTagAlias.tag_id == SiteTag.id)
            .where(SiteTagAlias.name_lower == key, SiteTag.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def disabled_name(session, name: str) -> SiteTagDisabled | None:
    key = name.strip().lower()
    if not key:
        return None
    return (
        await session.execute(
            select(SiteTagDisabled).where(SiteTagDisabled.name_lower == key)
        )
    ).scalar_one_or_none()


async def resolve_tag(session, raw: str) -> SiteTag | None:
    """tag 页的 ID：数字 ID；也接受同义词或名字（不分大小写，官方优先）。"""
    tag_id = parse_id(raw)
    if tag_id is not None:
        row = await session.get(SiteTag, tag_id)
        if row is not None and row.deleted_at is None:
            return row
        return None
    text = str(raw or "").strip()
    if text.startswith("free:"):
        text = text[5:]
    if not text:
        return None
    by_alias = await alias_target(session, text)
    if by_alias is not None:
        return by_alias
    rows = (
        (
            await session.execute(
                select(SiteTag).where(
                    func.lower(SiteTag.name) == text.lower(),
                    SiteTag.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    rows = sorted(rows, key=lambda t: (t.group_id == rules.FREE, t.id))
    return rows[0] if rows else None


def is_official(row: SiteTag) -> bool:
    return row.group_id != rules.FREE and bool(row.official)


def directory_item(
    row: SiteTag, counts: dict[int, int], alias_of: str | None = None
) -> dict:
    item = {
        "id": str(row.id),
        "name": row.name,
        "group": row.group_id,
        "official": is_official(row),
        "workCount": int(counts.get(row.id, 0)),
    }
    if alias_of:
        item["aliasOf"] = alias_of
    return item


async def tag_detail(
    session, ctx, row: SiteTag, counts: dict[int, int] | None = None
) -> dict:
    """协议 TagDetail（tag 页）。counts 不给时按「不看屏蔽 tag」现算。"""
    view_ctx = ctx_ignoring_blocked(ctx)
    conds = await svc.listable_conditions(session, view_ctx)
    if counts is None:
        counts = await visible_counts(session, ctx, ignore_blocked=True)

    # 相关 tag：和它一起出现最多的（只数可见作品），不算分级；资源类型另外数（tag 页的分类 tab）
    other = aliased(SiteWorkTag)
    co_rows = (
        await session.execute(
            select(SiteTag, func.count(func.distinct(Work.id)).label("n"))
            .select_from(SiteWorkTag)
            .join(Work, Work.id == SiteWorkTag.work_id)
            .join(other, other.work_id == SiteWorkTag.work_id)
            .join(SiteTag, SiteTag.id == other.tag_id)
            .where(
                SiteWorkTag.tag_id == row.id,
                SiteWorkTag.removed_at.is_(None),
                other.removed_at.is_(None),
                other.tag_id != row.id,
                SiteTag.deleted_at.is_(None),
                SiteTag.group_id != "rating",
                *conds,
            )
            .group_by(SiteTag.id)
            .order_by(func.count(func.distinct(Work.id)).desc(), SiteTag.id)
        )
    ).all()
    related = [directory_item(t, counts) for t, _ in co_rows[:10]]
    type_counts = sorted(
        ({"type": t.name, "count": int(n)} for t, n in co_rows if t.group_id == "type"),
        key=lambda x: (-x["count"], x["type"]),
    )
    if row.group_id == "type":
        type_counts = [{"type": row.name, "count": int(counts.get(row.id, 0))}]
        type_counts = [x for x in type_counts if x["count"] > 0]

    # 头图：周热度最高、封面在当前模式下能显示的作品
    cover = None
    candidates = (
        (
            await session.execute(
                select(Work)
                .outerjoin(WorkStatistics, WorkStatistics.work_id == Work.id)
                .where(
                    svc.has_tag(row.id),
                    Work.cover_attachment_id.is_not(None),
                    *conds,
                )
                .order_by(svc.heat_score("hot_week").desc(), Work.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    if candidates:
        media = await load_attachments(
            session, [w.cover_attachment_id for w in candidates]
        )
        for w in candidates:
            att = media.get(w.cover_attachment_id)
            if att is None:
                continue
            image = attachment_dto(att, ctx)
            if (
                image.get("kind") == "image"
                and not image.get("placeholder")
                and image.get("url")
            ):
                cover = {"workId": str(w.id), "workTitle": w.title, "image": image}
                break

    required_for: list[str] = []
    if is_official(row):
        for q in (
            await session.execute(
                select(SiteTagQuestion).where(
                    SiteTagQuestion.restricts_tag_id == row.id
                )
            )
        ).scalars():
            required_for.extend(q.for_types or [])
    viewer = ctx.viewer
    tid = str(row.id)
    return {
        "id": tid,
        "name": row.name,
        "group": row.group_id,
        "official": is_official(row),
        "restricts": bool(row.restricts),
        "description": row.description or "",
        "workCount": int(counts.get(row.id, 0)),
        "aliases": (await aliases_of(session, [row.id]))[row.id],
        "requiredQuestionFor": list(dict.fromkeys(required_for)),
        "related": related,
        "createdAt": iso_utc(row.created_at),
        "viewer": {
            "followed": bool(viewer and tid in viewer.prefs.followed_tags),
            "blocked": bool(viewer and tid in viewer.prefs.blocked_tags),
        },
        "cover": cover,
        "typeCounts": type_counts,
    }


async def bump_catalog_version(session) -> str:
    """改了 tag 目录后版本号 +1（前端据此刷新目录）。"""
    from sqlalchemy.dialects.postgresql import insert

    row = await session.get(SiteSetting, "tag_catalog", with_for_update=True)
    current = str(((row.value or {}) if row else {}).get("version") or "0")
    head, _, tail = current.rpartition(".")
    if head and tail.isdigit():
        version = f"{head}.{int(tail) + 1}"
    elif current.isdigit():
        version = str(int(current) + 1)
    else:
        version = f"{current}.1"
    if row is None:
        await session.execute(
            insert(SiteSetting)
            .values(key="tag_catalog", value={"version": version}, updated_at=utc_now())
            .on_conflict_do_nothing()
        )
    else:
        row.value = {**(row.value or {}), "version": version}
        row.updated_at = utc_now()
    return version
