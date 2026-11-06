"""协议 3.4：GET /tags/catalog（第一期）；以及第二期里用现有数据做的最简实现：
GET /tags/suggest、GET /boards、GET /boards/{id}、GET /search。

目录在 site_tag_group / site_tag / site_tag_question，maxTags 取配置 SITE_MAX_TAGS，
version 取 site_setting[tag_catalog]。作品上的 tag 在 site_work_tag。
所有作品列表按 services/works.listable_conditions 的 R18 规则过滤。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, literal, or_, select

from models.site_tag import SiteTag, SiteWorkTag
from models.user import User
from models.work import Work
from models.work_statistics import WorkStatistics
from site_v1.deps import Ctx, get_session_factory
from site_v1.errors import invalid, not_found
from site_v1.paging import PageRequest, pagination
from site_v1.routers.works import _sort, list_filters
from site_v1.services import tag_rules as rules
from site_v1.services import works as svc

router = APIRouter(tags=["tag 榜单搜索"])

# 有主榜的官方 tag 组（tag 方案：资源类型、受众、形态、题材各一个主榜）
BOARD_GROUPS = ("type", "audience", "form", "theme")
GLOBAL_BOARD = {"id": "all", "title": "全站", "tag": None}


@router.get("/tags/catalog", summary="官方 tag、必选规则、上限、限制属性和必答问题")
async def get_catalog():
    async with get_session_factory()() as session:
        return rules.catalog_dto(await rules.load_catalog(session))


@router.get(
    "/tags/suggest",
    summary="输入联想（第二期，最简实现：名字包含，作品数只数可见作品）",
)
async def suggest_tags(ctx: Ctx, q: str = ""):
    q = q.strip()
    if not q:
        return {"items": []}
    lower = q.lower()
    async with get_session_factory()() as session:
        candidates = (
            (
                await session.execute(
                    select(SiteTag)
                    .where(
                        SiteTag.deleted_at.is_(None),
                        func.lower(SiteTag.name).contains(lower, autoescape=True),
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        if not candidates:
            return {"items": []}
        conds = await svc.listable_conditions(session, ctx)
        counts = dict(
            (
                await session.execute(
                    select(SiteWorkTag.tag_id, func.count(func.distinct(Work.id)))
                    .join(Work, Work.id == SiteWorkTag.work_id)
                    .where(
                        SiteWorkTag.removed_at.is_(None),
                        SiteWorkTag.tag_id.in_([t.id for t in candidates]),
                        *conds,
                    )
                    .group_by(SiteWorkTag.tag_id)
                )
            ).all()
        )
    items = []
    for t in candidates:
        count = int(counts.get(t.id, 0))
        # 全年龄模式下不联想作品数为 0 的 tag（包括只出现在受限作品上的）；
        # r18 模式下官方 tag 照常联想，自由 tag 也要有可见作品才联想
        if count == 0 and (ctx.effective_mode == "general" or t.group_id == rules.FREE):
            continue
        items.append(
            {"id": str(t.id), "name": t.name, "group": t.group_id, "count": count}
        )
    items.sort(key=lambda x: (x["name"].lower() != lower, -x["count"]))
    return {"items": items[:10]}


# ---------------- 主榜 ----------------


async def _boards(session) -> list[dict]:
    catalog = await rules.load_catalog(session)
    boards = [dict(GLOBAL_BOARD)]
    for g in catalog.groups:
        if g.id not in BOARD_GROUPS:
            continue
        for t in g.tags:
            boards.append({"id": t.id, "title": t.name, "tag": svc.tag_ref(t, None)})
    return boards


@router.get(
    "/boards",
    summary="主榜列表（第二期，按目录生成：全站 + 类型/受众/形态/题材各 tag）",
)
async def list_boards():
    async with get_session_factory()() as session:
        return {"items": await _boards(session)}


@router.get("/boards/{id}", summary="榜单内容（参数同 /works）")
async def board_works(
    id: str,
    ctx: Ctx,
    sort: str | None = None,
    page: PageRequest = Depends(pagination),
    filters: dict = Depends(list_filters),
):
    sort = _sort(sort)
    filters.pop("tag_logic")
    async with get_session_factory()() as session:
        board = next((b for b in await _boards(session) if b["id"] == id), None)
        if board is None:
            raise not_found()
        if board["tag"] is not None:
            filters["tags"] = [board["tag"]["id"], *filters["tags"]]
        conds = await svc.listable_conditions(session, ctx, **filters)
        return await svc.query_page(session, ctx, conds, page, sort)


# ---------------- 搜索 ----------------


def _date(raw: str | None, field: str):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise invalid({field: "时间格式不对"})
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@router.get("/search", summary="作品搜索（第二期，最简实现：关键词包含匹配）")
async def search(
    request: Request,
    ctx: Ctx,
    q: str = "",
    sort: str | None = None,
    page: PageRequest = Depends(pagination),
    filters: dict = Depends(list_filters),
):
    qp = request.query_params
    words = [w.lower() for w in q.split() if w]
    sort = _sort(sort, svc.SEARCH_SORTS, "relevance" if words else "hot_week")
    async with get_session_factory()() as session:
        conds = await svc.listable_conditions(session, ctx, **filters)
        author_name = (
            select(func.coalesce(User.display_name, "") + " " + User.username)
            .where(User.id == Work.author_id)
            .scalar_subquery()
        )
        tag_names = (
            select(func.string_agg(SiteTag.name, " "))
            .select_from(SiteWorkTag)
            .join(SiteTag, SiteTag.id == SiteWorkTag.tag_id)
            .where(SiteWorkTag.work_id == Work.id, SiteWorkTag.removed_at.is_(None))
            .scalar_subquery()
        )
        haystack = func.lower(
            func.concat_ws(
                " ",
                Work.title,
                Work.summary,
                Work.description,
                author_name,
                tag_names,
            )
        )
        for word in words:
            conds.append(haystack.contains(word, autoescape=True))
        for word in svc.parse_csv(qp.getlist("excludeKeywords")):
            conds.append(
                ~func.lower(func.concat_ws(" ", Work.title, Work.summary)).contains(
                    word.lower(), autoescape=True
                )
            )
        authors = svc.parse_csv(qp.getlist("authors"))
        if authors:
            conds.append(Work.author_id.in_(authors))
        exclude_authors = svc.parse_csv(qp.getlist("excludeAuthors"))
        if exclude_authors:
            conds.append(
                or_(Work.author_id.is_(None), Work.author_id.not_in(exclude_authors))
            )
        min_fav = qp.get("minFavorites")
        if min_fav:
            try:
                threshold = max(0, int(min_fav))
            except ValueError:
                raise invalid({"minFavorites": "要是整数"})
            conds.append(
                select(WorkStatistics.collection_count)
                .where(WorkStatistics.work_id == Work.id)
                .scalar_subquery()
                >= threshold
            )
        for key, column, after in (
            ("createdAfter", Work.created_at, True),
            ("createdBefore", Work.created_at, False),
            ("updatedAfter", Work.updated_at, True),
            ("updatedBefore", Work.updated_at, False),
        ):
            value = _date(qp.get(key), key)
            if value is not None:
                conds.append(column >= value if after else column < value)
        relevance = None
        if words:
            title = func.lower(Work.title)
            relevance = sum(
                (case((title.contains(w, autoescape=True), 3), else_=0) for w in words),
                literal(0),
            )
        effective = {"new": "new", "favorites": "favorites"}.get(sort, sort)
        return await svc.query_page(
            session, ctx, conds, page, effective, relevance=relevance
        )
