"""书单（协议 3.18，协议叫 collections）：复用 V2 书单的数据（booklist / booklist_item / user_collection），
条目加删走 V2 WorkService.collect（和 V2 书单、作品收藏同一把锁、同样维护 item_count）。
默认收藏书单（is_default）是「收藏」，不算书单；赛事书单不出现。

R18（3.12）：条目里看不到的作品不返回、不计数；广场里可见条目为 0 的书单不出现；
封面图对当前模式不可见或取自看不到的作品时为 null；加入时作品必须对本人可见。
协议多出来的 tag、上传的封面、默认排序 updated 存在 site_collection_meta。
"""

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from models.attachment import Attachment
from models.booklist import Booklist
from models.booklist_item import BooklistItem
from models.site_me import SiteCollectionMeta
from models.site_tag import SiteTag
from models.user_collection import UserCollection
from models.work import Work
from shared.time_utils import utc_now
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import external_image, iso_utc, load_users
from site_v1.errors import forbidden, invalid, not_found
from site_v1.paging import PageRequest, page_response, pagination
from site_v1.services import tag_rules as rules
from site_v1.services.attachments import attachment_dto, load_attachments
from site_v1.services.work_access import get_visible_work, parse_id
from site_v1.services.works import listable_conditions, serialize_works, tag_ref

router = APIRouter(tags=["书单"])

SORTS = ("order", "added", "updated")
TITLE_MAX = 80
DESCRIPTION_MAX = 600
NOTE_MAX = 800
TAGS_MAX = 8


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch >= " " or ch in "\t\n\r")[:limit]


def _principal(viewer) -> dict:
    return {"id": viewer.user_id, "discord_user_id": viewer.discord_user_id}


def _is_admin(viewer) -> bool:
    return viewer is not None and viewer.has_permission("site.admin")


def _base_filters():
    return [
        Booklist.merged_into_id.is_(None),
        Booklist.is_tournament.is_(False),
        Booklist.is_default.is_(False),
    ]


async def _listable_work_ids(session, ctx):
    return select(Work.id).where(*(await listable_conditions(session, ctx)))


def _default_sort(row: Booklist, meta: SiteCollectionMeta | None) -> str:
    if meta is not None and meta.default_sort in SORTS:
        return meta.default_sort
    return "order" if row.default_sort_method == "display_order" else "added"


def _item_order(sort: str):
    if sort == "order":
        return [BooklistItem.display_order.asc(), BooklistItem.id.asc()]
    if sort == "updated":
        return [Work.updated_at.desc().nulls_last(), BooklistItem.id.desc()]
    return [BooklistItem.created_at.desc(), BooklistItem.id.desc()]


def _can_see(row: Booklist | None, viewer) -> bool:
    if row is None or row.merged_into_id or row.is_tournament or row.is_default:
        return False
    if row.is_public:
        return True
    return viewer is not None and (
        row.fluxer_owner_id == viewer.user_id or _is_admin(viewer)
    )


async def _visible_collection(session, ctx, raw_id, *, for_update=False) -> Booklist:
    bid = parse_id(raw_id)
    row = (
        await session.get(Booklist, bid, with_for_update=for_update or None)
        if bid
        else None
    )
    if not _can_see(row, ctx.viewer):
        raise not_found()
    return row


async def _own_collection(session, ctx, raw_id) -> Booklist:
    row = await _visible_collection(session, ctx, raw_id, for_update=True)
    if row.fluxer_owner_id != ctx.viewer.user_id:
        raise forbidden("只有创建者可以修改书单")
    return row


async def _metas(session, ids) -> dict[int, SiteCollectionMeta]:
    ids = list(ids)
    if not ids:
        return {}
    return {
        m.booklist_id: m
        for m in (
            await session.execute(
                select(SiteCollectionMeta).where(
                    SiteCollectionMeta.booklist_id.in_(ids)
                )
            )
        ).scalars()
    }


async def serialize_collections(session, ctx, rows: list[Booklist]) -> list[dict]:
    """批量转成协议 Collection（每类关联一次查询，封面按书单逐个取第一个可见条目）。"""
    if not rows:
        return []
    viewer = ctx.viewer
    ids = [r.id for r in rows]
    listable = await _listable_work_ids(session, ctx)
    counts = dict(
        (
            await session.execute(
                select(BooklistItem.booklist_id, func.count())
                .where(
                    BooklistItem.booklist_id.in_(ids),
                    BooklistItem.work_id.in_(listable),
                )
                .group_by(BooklistItem.booklist_id)
            )
        ).all()
    )
    favorited = set()
    if viewer is not None:
        favorited = set(
            (
                await session.execute(
                    select(UserCollection.target_id).where(
                        UserCollection.fluxer_user_id == viewer.user_id,
                        UserCollection.target_type == 2,
                        UserCollection.target_id.in_(ids),
                    )
                )
            ).scalars()
        )
    metas = await _metas(session, ids)
    tag_ids = {
        int(t) for m in metas.values() for t in (m.tag_ids or []) if str(t).isdigit()
    }
    tags = (
        {
            str(t.id): tag_ref(rules.tag_info_of(t), None)
            for t in (
                await session.execute(
                    select(SiteTag).where(
                        SiteTag.id.in_(tag_ids), SiteTag.deleted_at.is_(None)
                    )
                )
            ).scalars()
        }
        if tag_ids
        else {}
    )
    admin = _is_admin(viewer)
    show_owner = {
        r.id: (not r.is_anonymous)
        or (viewer is not None and r.fluxer_owner_id == viewer.user_id)
        or admin
        for r in rows
    }
    owners = await load_users(
        session,
        [r.fluxer_owner_id for r in rows if show_owner[r.id] and r.fluxer_owner_id],
    )
    # 封面：上传的封面（当前模式看不到时为 null）；没有时取第一个可见条目的作品封面
    uploaded = await load_attachments(
        session,
        [m.cover_attachment_id for m in metas.values() if m.cover_attachment_id],
    )
    covers: dict[int, dict | None] = {}
    for r in rows:
        meta = metas.get(r.id)
        cover = None
        if meta is not None and meta.cover_attachment_id:
            att = uploaded.get(meta.cover_attachment_id)
            dto = attachment_dto(att, ctx) if att else None
            cover = dto if dto and not dto.get("placeholder") else None
        elif r.cover_image_url:
            cover = external_image(f"booklist_cover_{r.id}", r.cover_image_url, "cover")
        else:
            first = (
                await session.execute(
                    select(Work.cover_attachment_id)
                    .join(BooklistItem, BooklistItem.work_id == Work.id)
                    .where(
                        BooklistItem.booklist_id == r.id,
                        Work.id.in_(listable),
                        Work.cover_attachment_id.is_not(None),
                    )
                    .order_by(*_item_order(_default_sort(r, meta)))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if first:
                att = (await load_attachments(session, [first])).get(first)
                dto = attachment_dto(att, ctx) if att else None
                cover = dto if dto and not dto.get("placeholder") else None
        covers[r.id] = cover
    result = []
    for r in rows:
        meta = metas.get(r.id)
        result.append(
            {
                "id": str(r.id),
                "owner": owners.get(r.fluxer_owner_id) if show_owner[r.id] else None,
                "title": r.title,
                "description": r.description or "",
                "cover": covers[r.id],
                "tags": [
                    tags[str(t)]
                    for t in (meta.tag_ids if meta else [])
                    if str(t) in tags
                ],
                "isPublic": bool(r.is_public),
                "isAnonymous": bool(r.is_anonymous),
                "defaultSort": _default_sort(r, meta),
                "itemCount": int(counts.get(r.id, 0)),
                "favoriteCount": int(r.collection_count or 0),
                "createdAt": iso_utc(r.created_at),
                "updatedAt": iso_utc(r.updated_at or r.created_at),
                "viewer": {
                    "favorited": r.id in favorited,
                    "canEdit": viewer is not None
                    and r.fluxer_owner_id == viewer.user_id,
                },
            }
        )
    return result


async def _apply_input(
    session, row: Booklist, meta: SiteCollectionMeta, body: dict, viewer, creating: bool
):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    if creating or "title" in body:
        title = _text(body.get("title"), TITLE_MAX).strip()
        if not title:
            raise invalid({"title": "请输入书单标题"})
        row.title = title
    if "description" in body:
        row.description = _text(body.get("description"), DESCRIPTION_MAX)
    for key, column in (("isPublic", "is_public"), ("isAnonymous", "is_anonymous")):
        if key in body:
            if not isinstance(body[key], bool):
                raise invalid({key: "只能是 true 或 false"})
            setattr(row, column, body[key])
    if "defaultSort" in body:
        sort = body["defaultSort"] if body["defaultSort"] in SORTS else "order"
        meta.default_sort = sort
        # V2 只认 join_time / display_order 两种；updated 在 V2 里按加入时间
        row.default_sort_method = "display_order" if sort == "order" else "join_time"
        row.default_sort_order = "asc" if sort == "order" else "desc"
    if isinstance(body.get("tags"), list):
        wanted = [str(t) for t in body["tags"] if isinstance(t, (str, int))]
        ids = [int(t) for t in wanted if t.isdigit()]
        valid = (
            set(
                str(i)
                for i in (
                    await session.execute(
                        select(SiteTag.id).where(
                            SiteTag.id.in_(ids), SiteTag.deleted_at.is_(None)
                        )
                    )
                ).scalars()
            )
            if ids
            else set()
        )
        meta.tag_ids = list(dict.fromkeys(t for t in wanted if t in valid))[:TAGS_MAX]
    if "coverUploadId" in body:
        raw = body.get("coverUploadId")
        if raw in (None, ""):
            meta.cover_attachment_id = None
        else:
            att_id = parse_id(raw)
            att = await session.get(Attachment, att_id) if att_id else None
            if (
                att is None
                or att.deleted_at is not None
                or att.owner_id != viewer.user_id
                or att.kind != "image"
                or att.state != "ready"
            ):
                raise invalid({"coverUploadId": "封面要是自己上传好的图片"})
            meta.cover_attachment_id = att.id
            row.cover_image_url = None
    row.updated_at = utc_now()


# ---------------- 书单广场、详情 ----------------


@router.get("/collections", summary="书单广场")
async def list_collections(
    ctx: Ctx,
    sort: str | None = Query(None),
    tags: str | None = Query(None),
    q: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    async with get_session_factory()() as session:
        listable = await _listable_work_ids(session, ctx)
        conds = [
            *_base_filters(),
            Booklist.is_public.is_(True),
            # 当前模式下可见条目为 0 的书单不出现
            select(BooklistItem.id)
            .where(
                BooklistItem.booklist_id == Booklist.id,
                BooklistItem.work_id.in_(listable),
            )
            .exists(),
        ]
        wanted = [t for t in (tags or "").split(",") if t]
        if wanted:
            conds.append(
                select(SiteCollectionMeta.booklist_id)
                .where(
                    SiteCollectionMeta.booklist_id == Booklist.id,
                    cast(SiteCollectionMeta.tag_ids, JSONB).contains(wanted),
                )
                .exists()
            )
        keyword = (q or "").strip()
        if keyword:
            pattern = f"%{keyword}%"
            conds.append(
                or_(Booklist.title.ilike(pattern), Booklist.description.ilike(pattern))
            )
        order = (
            [Booklist.created_at.desc(), Booklist.id.desc()]
            if sort == "new"
            else [Booklist.collection_count.desc(), Booklist.id.desc()]
        )
        rows = (
            (
                await session.execute(
                    select(Booklist)
                    .where(*conds)
                    .order_by(*order)
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        items = await serialize_collections(session, ctx, list(rows[: page.limit]))
    result = page_response(rows, page, lambda r: None)
    result["items"] = items
    return result


@router.post("/collections", summary="建书单", status_code=201)
async def create_collection(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    async with get_session_factory()() as session:
        discord = viewer.discord_user_id
        row = Booklist(
            title="",
            description="",
            is_public=True,
            is_anonymous=False,
            fluxer_owner_id=viewer.user_id,
            owner_id=int(discord) if discord and str(discord).isdigit() else None,
            default_sort_method="display_order",
            default_sort_order="asc",
        )
        meta = SiteCollectionMeta(booklist_id=0, default_sort="order")
        await _apply_input(session, row, meta, body, viewer, True)
        session.add(row)
        await session.flush()
        meta.booklist_id = row.id
        session.add(meta)
        await session.flush()
        result = (await serialize_collections(session, ctx, [row]))[0]
        await session.commit()
    return JSONResponse(status_code=201, content=result)


@router.get("/collections/{id}", summary="书单详情")
async def get_collection(id: str, ctx: Ctx):
    async with get_session_factory()() as session:
        row = await _visible_collection(session, ctx, id)
        return (await serialize_collections(session, ctx, [row]))[0]


@router.patch("/collections/{id}", summary="改书单（创建者）")
async def patch_collection(
    id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)
):
    async with get_session_factory()() as session:
        row = await _own_collection(session, ctx, id)
        meta = await session.get(SiteCollectionMeta, row.id)
        if meta is None:
            meta = SiteCollectionMeta(booklist_id=row.id)
            session.add(meta)
        await _apply_input(session, row, meta, body, viewer, False)
        await session.flush()
        result = (await serialize_collections(session, ctx, [row]))[0]
        await session.commit()
    return result


@router.delete("/collections/{id}", summary="删书单（创建者）", status_code=204)
async def delete_collection(id: str, viewer: CurrentUser, ctx: Ctx):
    from core.booklist_repository import BooklistRepository

    async with get_session_factory()() as session:
        row = await _own_collection(session, ctx, id)
        bid = row.id
        # 和 V2 WorkBooklistService.delete_booklist 同样的步骤
        await session.execute(
            delete(UserCollection).where(
                UserCollection.target_type == 2, UserCollection.target_id == bid
            )
        )
        await session.execute(
            delete(SiteCollectionMeta).where(SiteCollectionMeta.booklist_id == bid)
        )
        await BooklistRepository(session).delete_booklist(bid)
    return Response(status_code=204)


# ---------------- 条目 ----------------


def _item_dto(item: BooklistItem, work_dto: dict) -> dict:
    return {
        "work": work_dto,
        "note": item.comment or "",
        "order": int(item.display_order or 0),
        "addedAt": iso_utc(item.created_at),
    }


@router.get("/collections/{id}/items", summary="书单条目（只给当前模式能看到的）")
async def list_items(
    id: str,
    ctx: Ctx,
    sort: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    async with get_session_factory()() as session:
        row = await _visible_collection(session, ctx, id)
        meta = await session.get(SiteCollectionMeta, row.id)
        sort = sort if sort in SORTS else _default_sort(row, meta)
        pairs = (
            await session.execute(
                select(BooklistItem, Work)
                .join(Work, Work.id == BooklistItem.work_id)
                .where(
                    BooklistItem.booklist_id == row.id,
                    *(await listable_conditions(session, ctx)),
                )
                .order_by(*_item_order(sort))
                .offset(page.offset)
                .limit(page.limit + 1)
            )
        ).all()
        shown = pairs[: page.limit]
        works = await serialize_works(session, ctx, [w for _, w in shown])
    result = page_response(pairs, page, lambda p: None)
    result["items"] = [_item_dto(item, w) for (item, _), w in zip(shown, works)]
    return result


async def _addable_work(session, ctx, work_id) -> Work:
    """加入时作品必须对本人可见且已发布。"""
    try:
        work = await get_visible_work(session, ctx, work_id)
    except Exception:
        raise invalid({"workId": "找不到这个作品"})
    if work.status != "published":
        raise invalid({"workId": "找不到这个作品"})
    return work


async def _item_of(session, booklist_id: int, work_id: int) -> BooklistItem | None:
    return (
        (
            await session.execute(
                select(BooklistItem).where(
                    BooklistItem.booklist_id == booklist_id,
                    BooklistItem.work_id == work_id,
                )
            )
        )
        .scalars()
        .first()
    )


async def _add(
    session, viewer, row: Booklist, work: Work, note: str, order=None
) -> bool:
    """经 V2 WorkService.collect 加条目；已经在里面返回 False。"""
    from work.work_service import WorkService

    if await _item_of(session, row.id, work.id) is not None:
        return False
    await WorkService(session).collect(work, _principal(viewer), True, row.id)
    item = await _item_of(session, row.id, work.id)
    if order is None:
        maximum = (
            await session.execute(
                select(func.max(BooklistItem.display_order)).where(
                    BooklistItem.booklist_id == row.id, BooklistItem.id != item.id
                )
            )
        ).scalar_one() or 0
        order = maximum + 1
    item.display_order = int(order)
    item.comment = note or None
    row.updated_at = utc_now()
    return True


async def _remove(session, viewer, row: Booklist, work_id: int) -> bool:
    item = await _item_of(session, row.id, work_id)
    if item is None:
        return False
    await session.delete(item)
    row.item_count = max(0, (row.item_count or 0) - 1)
    row.updated_at = utc_now()
    return True


def _order(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid({"order": "排序要是整数"})
    value = int(round(value))
    if not -2147483648 <= value <= 2147483647:
        raise invalid({"order": "排序值太大"})
    return value


@router.post("/collections/{id}/items", summary="加条目（创建者）", status_code=201)
async def add_item(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    order = _order(body.get("order"))
    async with get_session_factory()() as session:
        row = await _own_collection(session, ctx, id)
        work = await _addable_work(session, ctx, body.get("workId"))
        if not await _add(
            session, viewer, row, work, _text(body.get("note"), NOTE_MAX), order
        ):
            raise invalid({"workId": "这个作品已经在书单里了"})
        await session.flush()
        result = (await serialize_collections(session, ctx, [row]))[0]
        await session.commit()
    return JSONResponse(status_code=201, content=result)


@router.patch(
    "/collections/{id}/items/{itemId}", summary="改条目点评和排序（itemId 是作品 ID）"
)
async def patch_item(
    id: str, itemId: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)
):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    order = _order(body.get("order"))
    async with get_session_factory()() as session:
        row = await _own_collection(session, ctx, id)
        work_id = parse_id(itemId)
        item = await _item_of(session, row.id, work_id) if work_id else None
        if item is None:
            raise not_found()
        if "note" in body:
            item.comment = _text(body.get("note"), NOTE_MAX) or None
        if order is not None:
            item.display_order = order
        row.updated_at = utc_now()
        await session.flush()
        work = await session.get(Work, work_id)
        work_dto = (await serialize_works(session, ctx, [work]))[0]
        result = _item_dto(item, work_dto)
        await session.commit()
    return result


@router.delete(
    "/collections/{id}/items/{itemId}", summary="移出条目（创建者）", status_code=204
)
async def delete_item(id: str, itemId: str, viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        row = await _own_collection(session, ctx, id)
        work_id = parse_id(itemId)
        if work_id:
            await _remove(session, viewer, row, work_id)
        await session.commit()
    return Response(status_code=204)


@router.post("/collections/sync", summary="一个作品同时加入/移出多个书单")
async def sync_collections(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    add = body.get("add") or []
    remove = body.get("remove") or []
    if (
        not isinstance(add, list)
        or not isinstance(remove, list)
        or len(add) + len(remove) > 100
    ):
        raise invalid({"add": "要是书单 ID 列表"})
    note = _text(body.get("note"), NOTE_MAX)
    result = {"added": [], "removed": [], "unchanged": []}
    async with get_session_factory()() as session:
        work = await _addable_work(session, ctx, body.get("workId"))
        # 和 V2 sync_item 一样按编号顺序锁书单
        rows = {}
        for raw in sorted({str(x) for x in [*add, *remove]}):
            rows[raw] = await _own_collection(session, ctx, raw)
        for raw in add:
            row = rows[str(raw)]
            if await _add(session, viewer, row, work, note):
                result["added"].append(str(raw))
            else:
                if note:
                    item = await _item_of(session, row.id, work.id)
                    item.comment = note
                result["unchanged"].append(str(raw))
        for raw in remove:
            row = rows[str(raw)]
            if await _remove(session, viewer, row, work.id):
                result["removed"].append(str(raw))
            else:
                result["unchanged"].append(str(raw))
        await session.commit()
    return result


# ---------------- 收藏书单 ----------------


async def _favorite(id: str, viewer, ctx, on: bool):
    """和 V2 collect_booklist 写同一份 user_collection（target_type=2）和 collection_count；
    没有 Discord 绑定时 user_id 留空。"""
    async with get_session_factory()() as session:
        row = await _visible_collection(session, ctx, id, for_update=True)
        existing = (
            (
                await session.execute(
                    select(UserCollection).where(
                        UserCollection.target_type == 2,
                        UserCollection.target_id == row.id,
                        UserCollection.fluxer_user_id == viewer.user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if on and not existing:
            discord = viewer.discord_user_id
            session.add(
                UserCollection(
                    target_type=2,
                    target_id=row.id,
                    fluxer_user_id=viewer.user_id,
                    user_id=int(discord)
                    if discord and str(discord).isdigit()
                    else None,
                )
            )
            row.collection_count = (row.collection_count or 0) + 1
        elif not on and existing:
            for item in existing:
                await session.delete(item)
            row.collection_count = max(0, (row.collection_count or 0) - len(existing))
        await session.flush()
        result = (await serialize_collections(session, ctx, [row]))[0]
        await session.commit()
    return result


@router.post("/collections/{id}/favorite", summary="收藏书单")
async def favorite_collection(id: str, viewer: CurrentUser, ctx: Ctx):
    return await _favorite(id, viewer, ctx, True)


@router.delete("/collections/{id}/favorite", summary="取消收藏书单")
async def unfavorite_collection(id: str, viewer: CurrentUser, ctx: Ctx):
    return await _favorite(id, viewer, ctx, False)


# ---------------- 我的书单、某人的书单 ----------------


@router.get("/me/collections", summary="我的书单 / 我收藏的书单")
async def my_collections(
    viewer: CurrentUser,
    ctx: Ctx,
    kind: str | None = Query(None),
    containsWork: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    conds = list(_base_filters())
    if kind == "favorited":
        conds.append(
            Booklist.id.in_(
                select(UserCollection.target_id).where(
                    UserCollection.fluxer_user_id == viewer.user_id,
                    UserCollection.target_type == 2,
                )
            )
        )
        if not _is_admin(viewer):
            conds.append(
                or_(
                    Booklist.is_public.is_(True),
                    Booklist.fluxer_owner_id == viewer.user_id,
                )
            )
    else:
        conds.append(Booklist.fluxer_owner_id == viewer.user_id)
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Booklist)
                    .where(*conds)
                    .order_by(Booklist.updated_at.desc(), Booklist.id.desc())
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        shown = list(rows[: page.limit])
        items = await serialize_collections(session, ctx, shown)
        work_id = parse_id(containsWork) if containsWork else None
        if containsWork is not None:
            having = (
                set(
                    (
                        await session.execute(
                            select(BooklistItem.booklist_id).where(
                                BooklistItem.booklist_id.in_([r.id for r in shown]),
                                BooklistItem.work_id == (work_id or -1),
                            )
                        )
                    ).scalars()
                )
                if shown
                else set()
            )
            for r, item in zip(shown, items):
                item["containsWork"] = r.id in having
    result = page_response(rows, page, lambda r: None)
    result["items"] = items
    return result


@router.get("/users/{id}/collections", summary="某人的公开书单（不含匿名的）")
async def user_collections(id: str, ctx: Ctx, page: PageRequest = Depends(pagination)):
    async with get_session_factory()() as session:
        listable = await _listable_work_ids(session, ctx)
        rows = (
            (
                await session.execute(
                    select(Booklist)
                    .where(
                        *_base_filters(),
                        Booklist.fluxer_owner_id == id,
                        Booklist.is_public.is_(True),
                        Booklist.is_anonymous.is_(False),
                        select(BooklistItem.id)
                        .where(
                            BooklistItem.booklist_id == Booklist.id,
                            BooklistItem.work_id.in_(listable),
                        )
                        .exists(),
                    )
                    .order_by(Booklist.updated_at.desc(), Booklist.id.desc())
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        items = await serialize_collections(session, ctx, list(rows[: page.limit]))
    result = page_response(rows, page, lambda r: None)
    result["items"] = items
    return result
