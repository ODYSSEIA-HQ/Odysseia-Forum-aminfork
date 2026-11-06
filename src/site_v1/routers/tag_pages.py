"""tag 浏览与治理（草案 G）：
- GET /tags（标签池、tag 目录，含自由 tag）、GET /tags/{id}（tag 页）、关注 / 屏蔽 tag；
- PUT /works/{id}/tag-lock、GET /works/{id}/tag-log；
- /admin/tags*（tags.manage）：建官方 tag、改名和属性、同义词、合并、禁用、自由 tag 升官方、升级候选。

对齐假后端 src/mock/handlers/tags.ts、tagAdmin.ts。用 /api/v1 的协议 tag 表（site_tag*），
不碰 V1/V2 的 7 类标签。tag 记录直接来自 site_work_tag（移除保留记录）。
要注册在 routers/tags.py 之后（/tags/catalog、/tags/suggest 优先于 /tags/{id}）。
"""

from datetime import timedelta

from fastapi import APIRouter, Body, Depends
from sqlalchemy import Text, case, cast, func, select, update

from models.site_account import SiteUserPrefs
from models.site_nav import SiteTagAlias, SiteTagDisabled
from models.site_tag import SiteTag, SiteTagQuestion, SiteWorkTag
from models.work import Work
from shared.time_utils import utc_now
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import iso_utc, load_users, me_dto
from site_v1.errors import forbidden, invalid, not_found
from site_v1.paging import PageRequest, page_request, page_response, pagination
from site_v1.routers.me import _reload
from site_v1.services import tag_directory as td
from site_v1.services import tag_rules as rules
from site_v1.services import works as svc
from site_v1.services.work_access import can_manage_work, get_visible_work

router = APIRouter(tags=["tag 治理"])

MAX_SYNONYMS = 30
MAX_DESCRIPTION = 2000


# ---------------- 标签池、tag 页 ----------------


@router.get("/tags", summary="标签池、tag 目录（含自由 tag）")
async def list_tags(
    ctx: Ctx,
    group: str | None = None,
    q: str = "",
    sort: str = "popular",
    cursor: str | None = None,
    limit: str | None = None,
):
    if group and group not in td.TAG_GROUPS:
        raise invalid({"group": "没有这个 tag 组"})
    if sort not in ("popular", "new"):
        raise invalid({"sort": "sort 只能是 popular 或 new"})
    # 标签池一页默认 60 个（同假后端）
    page = page_request(cursor, limit if limit is not None else 60)
    needle = q.strip().lower()
    async with get_session_factory()() as session:
        rows = await td.active_tags(session)
        counts = await td.visible_counts(session, ctx)
        aliases: dict[int, list[str]] = {}
        if needle:
            for tag_id, name in await td.all_aliases(session):
                aliases.setdefault(tag_id, []).append(name)
    items = []
    for t in rows:
        if group and t.group_id != group:
            continue
        alias_of = None
        if needle and needle not in t.name.lower():
            alias_of = next(
                (a for a in aliases.get(t.id, []) if needle in a.lower()), None
            )
            if alias_of is None:
                continue
        item = td.directory_item(t, counts, alias_of)
        # 全年龄模式下作品数为 0 的 tag 不返回
        if ctx.effective_mode == "general" and item["workCount"] == 0:
            continue
        items.append((item, t.created_at))
    if sort == "new":
        items.sort(key=lambda x: (x[1], x[0]["workCount"]), reverse=True)
    else:
        items.sort(key=lambda x: (-x[0]["workCount"], x[0]["name"]))
    chunk = items[page.offset : page.offset + page.limit + 1]
    return page_response(chunk, page, lambda x: x[0])


async def _visible_tag(session, ctx, raw: str) -> tuple[SiteTag, dict[int, int]]:
    """当前查看者能看到的 tag；看不到（全年龄模式下作品数为 0）一律 404。"""
    row = await td.resolve_tag(session, raw)
    if row is None:
        raise not_found()
    counts = await td.visible_counts(session, ctx, ignore_blocked=True)
    if ctx.effective_mode == "general" and not counts.get(row.id):
        raise not_found()
    return row, counts


@router.get("/tags/{id}", summary="tag 页信息")
async def get_tag(id: str, ctx: Ctx):
    async with get_session_factory()() as session:
        row, counts = await _visible_tag(session, ctx, id)
        return await td.tag_detail(session, ctx, row, counts)


async def _toggle_pref(ctx, raw: str, key: str, on: bool):
    viewer = ctx.require_user()
    async with get_session_factory()() as session:
        if on:
            row, _ = await _visible_tag(session, ctx, raw)
            tag_id = str(row.id)
        else:
            # 取消时 tag 可能已经看不到了（例如屏蔽后作品数为 0），只要能认出来就行
            row = await td.resolve_tag(session, raw)
            tag_id = str(row.id) if row is not None else str(raw)
        prefs = await session.get(SiteUserPrefs, viewer.user_id, with_for_update=True)
        if prefs is None:
            raise not_found()
        column = "followed_tag_ids" if key == "followed" else "blocked_tag_ids"
        other = "blocked_tag_ids" if key == "followed" else "followed_tag_ids"
        current = [x for x in (getattr(prefs, column) or []) if x != tag_id]
        setattr(prefs, column, [*current, tag_id] if on else current)
        if on:
            # 关注和屏蔽互斥
            setattr(
                prefs, other, [x for x in (getattr(prefs, other) or []) if x != tag_id]
            )
        prefs.updated_at = utc_now()
        await session.commit()
    await _reload(ctx)
    return me_dto(ctx.viewer)


@router.post("/tags/{id}/follow", summary="关注 tag")
async def follow_tag(id: str, ctx: Ctx, viewer: CurrentUser):
    return await _toggle_pref(ctx, id, "followed", True)


@router.delete("/tags/{id}/follow", summary="取消关注 tag")
async def unfollow_tag(id: str, ctx: Ctx, viewer: CurrentUser):
    return await _toggle_pref(ctx, id, "followed", False)


@router.post("/tags/{id}/block", summary="屏蔽 tag")
async def block_tag(id: str, ctx: Ctx, viewer: CurrentUser):
    return await _toggle_pref(ctx, id, "blocked", True)


@router.delete("/tags/{id}/block", summary="取消屏蔽 tag")
async def unblock_tag(id: str, ctx: Ctx, viewer: CurrentUser):
    return await _toggle_pref(ctx, id, "blocked", False)


# ---------------- 作品上的 tag 记录和锁定 ----------------


def _is_tag_staff(viewer) -> bool:
    return any(
        viewer.has_permission(k)
        for k in ("works.manage_any", "tags.manage", "moderation.content")
    )


@router.get(
    "/works/{id}/tag-log", summary="作品上每个 tag 的增删记录（作者/管理/站务）"
)
async def tag_log(
    id: str, ctx: Ctx, viewer: CurrentUser, page: PageRequest = Depends(pagination)
):
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id)
        if not can_manage_work(work, viewer) and not _is_tag_staff(viewer):
            raise forbidden("只有作者、卡贴管理员和站务能看 tag 记录")
        rows = (
            await session.execute(
                select(SiteWorkTag, SiteTag.name)
                .join(SiteTag, SiteTag.id == SiteWorkTag.tag_id)
                .where(SiteWorkTag.work_id == work.id)
            )
        ).all()
        entries = []
        for r, name in rows:
            entries.append(
                (r.created_at, r.id, 0, str(r.tag_id), name, "add", r.added_by)
            )
            if r.removed_at is not None:
                entries.append(
                    (r.removed_at, r.id, 1, str(r.tag_id), name, "remove", r.removed_by)
                )
        entries.sort(key=lambda e: (e[0], e[1], e[2]), reverse=True)
        # 系统自动加的、没有记录操作者的算作者
        users = await load_users(
            session, {e[6] or work.author_id for e in entries if e[6] or work.author_id}
        )
    chunk = entries[page.offset : page.offset + page.limit + 1]

    def dto(e):
        by = e[6] or work.author_id
        return {
            "tagId": e[3],
            "tagName": e[4],
            "action": e[5],
            "by": users.get(by) or svc.unknown_user(by),
            "at": iso_utc(e[0]),
        }

    return page_response(chunk, page, dto)


@router.put("/works/{id}/tag-lock", summary="锁定 tag，禁止他人加 tag（作者）")
async def tag_lock(id: str, ctx: Ctx, viewer: CurrentUser, body: dict = Body(...)):
    locked = body.get("locked") if isinstance(body, dict) else None
    async with get_session_factory()() as session:
        work = await get_visible_work(session, ctx, id, for_update=True)
        if work.author_id != viewer.user_id and not (
            viewer.has_permission("works.manage_any")
            or viewer.has_permission("tags.manage")
        ):
            raise forbidden("只有作者能锁定 tag")
        if not isinstance(locked, bool):
            raise invalid({"locked": "要传 true 或 false"})
        work.tag_locked = locked
        work.updated_at = utc_now()
        await session.commit()
        await session.refresh(work)
        return await svc.serialize_work(session, ctx, work)


# ---------------- 管理后台 ----------------


def _require_tag_manager(ctx):
    viewer = ctx.require_user()
    if not viewer.has_permission("tags.manage"):
        raise forbidden("你没有管理 tag 的权限")
    return viewer


async def _existing(session, raw: str, *, lock: bool = False) -> SiteTag:
    tag_id = svc.parse_id(raw)
    row = (
        await session.get(SiteTag, tag_id, with_for_update=lock or None)
        if tag_id
        else None
    )
    if row is None or row.deleted_at is not None:
        raise not_found()
    return row


async def _check_name(session, name, except_id: int | None = None) -> str:
    """名字能不能用：合法、没被占用（官方、自由、同义词）、没被禁用。"""
    value = name.strip() if isinstance(name, str) else ""
    error = rules.validate_free_tag_name(value)
    if error:
        raise invalid({"name": error}, error)
    lower = value.lower()
    taken = (
        (
            await session.execute(
                select(SiteTag).where(
                    func.lower(SiteTag.name) == lower, SiteTag.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    taken = [t for t in taken if t.id != except_id]
    if taken:
        raise invalid({"name": f"已经有「{taken[0].name}」这个 tag 了"})
    alias = await td.alias_target(session, value)
    if alias is not None and alias.id != except_id:
        raise invalid({"name": f"「{value}」是「{alias.name}」的同义词"})
    if await td.disabled_name(session, value) is not None:
        raise invalid({"name": f"「{value}」已被禁用，不能再建"})
    return value


async def _set_required_question(session, tag: SiteTag, for_types) -> None:
    if for_types is None:
        return
    if not isinstance(for_types, list) or any(
        not isinstance(t, str) for t in for_types
    ):
        raise invalid({"requiredQuestionFor": "要是资源类型名列表"})
    type_rows = (
        await session.execute(
            select(SiteTag.id, SiteTag.name).where(
                SiteTag.group_id == "type", SiteTag.deleted_at.is_(None)
            )
        )
    ).all()
    types = {name for _, name in type_rows}
    # 协议只写了 string：资源类型名（前端、假后端的写法）之外，也认资源类型 tag 的 ID 和「type:名字」
    by_ref = {str(i): name for i, name in type_rows}
    by_ref.update({f"type:{name}": name for _, name in type_rows})
    for_types = [t if t in types else by_ref.get(t.strip(), t) for t in for_types]
    bad = [t for t in for_types if t not in types]
    if bad:
        raise invalid({"requiredQuestionFor": f"没有这些资源类型：{'、'.join(bad)}"})
    wanted = list(dict.fromkeys(for_types))
    existing = (
        (
            await session.execute(
                select(SiteTagQuestion).where(
                    SiteTagQuestion.restricts_tag_id == tag.id
                )
            )
        )
        .scalars()
        .all()
    )
    if not wanted:
        for q in existing:
            await session.delete(q)
        return
    if existing:
        existing[0].for_types = wanted
        for q in existing[1:]:
            await session.delete(q)
        return
    max_order = (
        await session.execute(
            select(func.coalesce(func.max(SiteTagQuestion.sort_order), 0))
        )
    ).scalar_one()
    session.add(
        SiteTagQuestion(
            id=f"q_{tag.id}",
            text=f"这个作品是否含「{tag.name}」内容？",
            hint=f"含「{tag.name}」内容的作品会自动打上这个 tag，只对能看到它的成员显示。",
            for_types=wanted,
            restricts_tag_id=tag.id,
            sort_order=int(max_order) + 1,
        )
    )


async def _detail(session, ctx, tag: SiteTag) -> dict:
    await session.flush()
    return await td.tag_detail(session, ctx, tag)


async def _refresh_visibility(session, work_ids) -> None:
    """tag 的 restricts 变了、作品换了 tag 之后，同步 work.visibility 冗余列。"""
    ids = list({int(i) for i in work_ids})
    if not ids:
        return
    await session.execute(
        update(Work)
        .where(Work.id.in_(ids))
        .values(
            visibility=case(
                (
                    Work.is_nsfw.is_(True)
                    | Work.is_abyss.is_(True)
                    | svc.restricting_tag_exists(),
                    "restricted",
                ),
                else_="public",
            )
        )
        .execution_options(synchronize_session=False)
    )


async def _tag_work_ids(session, tag_id: int) -> list[int]:
    return list(
        (
            await session.execute(
                select(SiteWorkTag.work_id).where(
                    SiteWorkTag.tag_id == tag_id, SiteWorkTag.removed_at.is_(None)
                )
            )
        ).scalars()
    )


async def _replace_everywhere(
    session, source: int, target: int | None, actor: str
) -> int:
    """把所有作品上的 source 换成 target（None 为直接去掉），同时改关注和屏蔽；返回改动的作品数。"""
    now = utc_now()
    rows = (
        (
            await session.execute(
                select(SiteWorkTag).where(
                    SiteWorkTag.tag_id == source, SiteWorkTag.removed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    work_ids = [r.work_id for r in rows]
    having_target: set[int] = set()
    if target is not None and work_ids:
        having_target = set(
            (
                await session.execute(
                    select(SiteWorkTag.work_id).where(
                        SiteWorkTag.tag_id == target,
                        SiteWorkTag.removed_at.is_(None),
                        SiteWorkTag.work_id.in_(work_ids),
                    )
                )
            ).scalars()
        )
    for r in rows:
        r.removed_at = now
        r.removed_by = actor
    await session.flush()
    for r in rows:
        if target is not None and r.work_id not in having_target:
            session.add(
                SiteWorkTag(
                    work_id=r.work_id, tag_id=target, added_by=actor, created_at=now
                )
            )
    if work_ids:
        await session.execute(
            update(Work)
            .where(Work.id.in_(work_ids))
            .values(updated_at=now)
            .execution_options(synchronize_session=False)
        )
    await session.flush()
    await _refresh_visibility(session, work_ids)
    # 关注和屏蔽跟着换
    src, dst = str(source), (str(target) if target is not None else None)
    needle = f'"{src}"'
    prefs_rows = (
        (
            await session.execute(
                select(SiteUserPrefs).where(
                    cast(SiteUserPrefs.followed_tag_ids, Text).contains(needle)
                    | cast(SiteUserPrefs.blocked_tag_ids, Text).contains(needle)
                )
            )
        )
        .scalars()
        .all()
    )
    for p in prefs_rows:
        for column in ("followed_tag_ids", "blocked_tag_ids"):
            values = list(getattr(p, column) or [])
            if src not in values:
                continue
            values = [
                dst if x == src else x for x in values if dst is not None or x != src
            ]
            setattr(p, column, list(dict.fromkeys(values)))
    return len(work_ids)


async def _repoint_aliases(session, source: int, target: int) -> None:
    await session.execute(
        update(SiteTagAlias).where(SiteTagAlias.tag_id == source).values(tag_id=target)
    )


async def _add_alias(session, tag_id: int, name: str, actor: str) -> None:
    from sqlalchemy.dialects.postgresql import insert

    await session.execute(
        insert(SiteTagAlias)
        .values(
            tag_id=tag_id,
            name=name,
            name_lower=name.lower(),
            created_by=actor,
            created_at=utc_now(),
        )
        .on_conflict_do_update(
            index_elements=[SiteTagAlias.name_lower], set_={"tag_id": tag_id}
        )
    )


def _tag_and_result(detail: dict, affected: int = 0) -> dict:
    """POST /admin/tags 和 POST /admin/tags/{id}/synonyms 的响应：协议和前端对不上，两种形状都给。

    - 协议（openapi.yaml）：建 tag 返回 TagDetail，同义词返回 {tag, affectedWorks}；
    - 前端（src/features/tag-admin/api.ts、假后端 tagAdmin.ts）：建 tag 读 {tag, affectedWorks}，
      同义词读 TagDetail。
    所以返回 TagDetail 的全部字段，再加上 tag（同一个 TagDetail）和 affectedWorks：
    按任一种形状读都对，两边都不用改。等协议和前端统一后去掉多余的一半。
    """
    return {**detail, "tag": detail, "affectedWorks": affected}


@router.post("/admin/tags", status_code=201, summary="建官方 tag")
async def create_tag(ctx: Ctx, body: dict = Body(...)):
    viewer = _require_tag_manager(ctx)
    group = body.get("group")
    if group not in td.OFFICIAL_GROUPS:
        raise invalid({"group": "请选择官方 tag 组"})
    if group == "rating":
        raise invalid({"group": "分级只有全年龄、R18、R18G 三个，不能新建"})
    async with get_session_factory()() as session:
        name = await _check_name(session, body.get("name"))
        max_order = (
            await session.execute(
                select(func.coalesce(func.max(SiteTag.sort_order), 0)).where(
                    SiteTag.group_id == group
                )
            )
        ).scalar_one()
        description = body.get("description")
        tag = SiteTag(
            group_id=group,
            name=name,
            restricts=bool(body.get("restricts")),
            official=True,
            description=(description.strip() if isinstance(description, str) else "")[
                :MAX_DESCRIPTION
            ],
            sort_order=int(max_order) + 1,
            created_by=viewer.user_id,
            created_at=utc_now(),
        )
        session.add(tag)
        await session.flush()
        await _set_required_question(session, tag, body.get("requiredQuestionFor"))
        await td.bump_catalog_version(session)
        detail = await _detail(session, ctx, tag)
        await session.commit()
    return _tag_and_result(detail, 0)


@router.patch("/admin/tags/{id}", summary="改名、简介、restricts、必答类型")
async def patch_tag(id: str, ctx: Ctx, body: dict = Body(...)):
    viewer = _require_tag_manager(ctx)
    async with get_session_factory()() as session:
        tag = await _existing(session, id, lock=True)
        official = td.is_official(tag)
        if ("restricts" in body or "requiredQuestionFor" in body) and not official:
            raise invalid(
                {"restricts": "自由 tag 不能设限制可见和必答问题，先升为官方 tag"}
            )
        name = body.get("name")
        if isinstance(name, str) and name.strip() != tag.name:
            if tag.group_id in td.STRUCTURAL_GROUPS:
                raise invalid({"name": "分级和资源类型的名字系统要用，不能改"})
            new_name = await _check_name(session, name, tag.id)
            old_name = tag.name
            tag.name = new_name
            if not official:
                # 自由 tag 改名：旧名留作同义词（同假后端）
                await _add_alias(session, tag.id, old_name, viewer.user_id)
        if official and "restricts" in body:
            if tag.group_id == "rating":
                raise invalid({"restricts": "分级 tag 的限制可见属性不能改"})
            if not isinstance(body["restricts"], bool):
                raise invalid({"restricts": "要是 true 或 false"})
            if bool(tag.restricts) != body["restricts"]:
                tag.restricts = body["restricts"]
                await session.flush()
                await _refresh_visibility(session, await _tag_work_ids(session, tag.id))
        if "description" in body:
            desc = body["description"]
            if not isinstance(desc, str):
                raise invalid({"description": "简介要是文字"})
            tag.description = desc.strip()[:MAX_DESCRIPTION]
        if official:
            await _set_required_question(session, tag, body.get("requiredQuestionFor"))
        await td.bump_catalog_version(session)
        detail = await _detail(session, ctx, tag)
        await session.commit()
    return detail


@router.post("/admin/tags/{id}/synonyms", summary="整组替换同义词")
async def set_synonyms(id: str, ctx: Ctx, body: dict = Body(...)):
    viewer = _require_tag_manager(ctx)
    names = body.get("names")
    if not isinstance(names, list) or len(names) > MAX_SYNONYMS:
        raise invalid({"names": f"同义词最多 {MAX_SYNONYMS} 个"})
    clean = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
    async with get_session_factory()() as session:
        tag = await _existing(session, id, lock=True)
        seen: set[str] = set()
        for n in clean:
            lower = n.lower()
            if lower in seen:
                continue
            seen.add(lower)
            if len(n) > rules.MAX_FREE_TAG_LENGTH:
                raise invalid(
                    {"names": f"「{n}」超过 {rules.MAX_FREE_TAG_LENGTH} 个字"}
                )
            if lower == tag.name.lower():
                raise invalid({"names": "同义词不能和主名一样"})
            owner = (
                await session.execute(
                    select(SiteTag.id).where(
                        func.lower(SiteTag.name) == lower, SiteTag.deleted_at.is_(None)
                    )
                )
            ).first()
            if owner is not None:
                raise invalid({"names": f"「{n}」本身是一个 tag，要用合并"})
            other = await td.alias_target(session, n)
            if other is not None and other.id != tag.id:
                raise invalid({"names": f"「{n}」已经是「{other.name}」的同义词"})
        await session.execute(
            SiteTagAlias.__table__.delete().where(SiteTagAlias.tag_id == tag.id)
        )
        await session.flush()
        done: set[str] = set()
        for n in clean:
            if n.lower() in done:
                continue
            done.add(n.lower())
            await _add_alias(session, tag.id, n, viewer.user_id)
        detail = await _detail(session, ctx, tag)
        await session.commit()
    return _tag_and_result(detail, 0)


@router.post("/admin/tags/{id}/merge", summary="把这个 tag 并进另一个，原名变成同义词")
async def merge_tag(id: str, ctx: Ctx, body: dict = Body(...)):
    viewer = _require_tag_manager(ctx)
    target_raw = body.get("targetId")
    if not target_raw:
        raise invalid({"targetId": "请选择要并进的 tag"})
    async with get_session_factory()() as session:
        source = await _existing(session, id, lock=True)
        target = await _existing(session, str(target_raw), lock=True)
        if target.id == source.id:
            raise invalid({"targetId": "不能并进自己"})
        if source.group_id in td.STRUCTURAL_GROUPS:
            raise invalid({"targetId": "分级和资源类型 tag 不能合并"})
        if target.group_id == "rating":
            raise invalid({"targetId": "不能并进分级 tag"})
        if source.restricts and not target.restricts:
            raise invalid(
                {
                    "targetId": "限制可见的 tag 只能并进同样限制可见的 tag，否则作品会变成公开"
                }
            )
        affected = await _replace_everywhere(
            session, source.id, target.id, viewer.user_id
        )
        source.deleted_at = utc_now()
        await session.execute(
            SiteTagQuestion.__table__.delete().where(
                SiteTagQuestion.restricts_tag_id == source.id
            )
        )
        await _repoint_aliases(session, source.id, target.id)
        await session.flush()
        await _add_alias(session, target.id, source.name, viewer.user_id)
        await td.bump_catalog_version(session)
        detail = await _detail(session, ctx, target)
        await session.commit()
    return {"tag": detail, "affectedWorks": affected}


@router.post("/admin/tags/{id}/disable", summary="禁用：从所有作品上移除，之后不能再建")
async def disable_tag(id: str, ctx: Ctx, body: dict = Body(...)):
    viewer = _require_tag_manager(ctx)
    reason = body.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    async with get_session_factory()() as session:
        tag = await _existing(session, id, lock=True)
        if not reason:
            raise invalid({"reason": "请写禁用理由"})
        if tag.group_id in td.STRUCTURAL_GROUPS:
            raise invalid({"reason": "分级和资源类型 tag 不能禁用"})
        affected = await _replace_everywhere(session, tag.id, None, viewer.user_id)
        tag.deleted_at = utc_now()
        await session.execute(
            SiteTagQuestion.__table__.delete().where(
                SiteTagQuestion.restricts_tag_id == tag.id
            )
        )
        await session.execute(
            SiteTagAlias.__table__.delete().where(SiteTagAlias.tag_id == tag.id)
        )
        from sqlalchemy.dialects.postgresql import insert

        await session.execute(
            insert(SiteTagDisabled)
            .values(
                tag_id=tag.id,
                name=tag.name,
                name_lower=tag.name.lower(),
                reason=reason[:500],
                disabled_by=viewer.user_id,
                disabled_at=utc_now(),
            )
            .on_conflict_do_nothing()
        )
        await td.bump_catalog_version(session)
        await session.commit()
    return {"tag": None, "affectedWorks": affected}


@router.post("/admin/tags/{id}/promote", summary="自由 tag 升为官方")
async def promote_tag(id: str, ctx: Ctx, body: dict = Body(...)):
    viewer = _require_tag_manager(ctx)
    group = body.get("group")
    async with get_session_factory()() as session:
        tag = await _existing(session, id, lock=True)
        if td.is_official(tag):
            raise invalid({"group": "已经是官方 tag 了"})
        if group not in td.OFFICIAL_GROUPS or group in td.STRUCTURAL_GROUPS:
            raise invalid({"group": "请选择要放进的官方组（分级、资源类型除外）"})
        name = body.get("name")
        final = tag.name
        if isinstance(name, str) and name.strip() and name.strip() != tag.name:
            final = await _check_name(session, name, tag.id)
        clash = (
            await session.execute(
                select(SiteTag.id).where(
                    SiteTag.group_id == group,
                    func.lower(SiteTag.name) == final.lower(),
                    SiteTag.deleted_at.is_(None),
                    SiteTag.id != tag.id,
                )
            )
        ).first()
        if clash is not None:
            raise invalid({"name": f"「{group}」组里已经有「{final}」了，请用合并"})
        max_order = (
            await session.execute(
                select(func.coalesce(func.max(SiteTag.sort_order), 0)).where(
                    SiteTag.group_id == group
                )
            )
        ).scalar_one()
        old_name = tag.name
        # ID 不变（作品上的绑定、关注、屏蔽都不用改），只换组
        tag.group_id = group
        tag.official = True
        tag.name = final
        tag.sort_order = int(max_order) + 1
        if final != old_name:
            await _add_alias(session, tag.id, old_name, viewer.user_id)
        affected = len(await _tag_work_ids(session, tag.id))
        await td.bump_catalog_version(session)
        detail = await _detail(session, ctx, tag)
        await session.commit()
    return {"tag": detail, "affectedWorks": affected}


@router.get("/admin/tags/promote-candidates", summary="自由 tag 升级候选")
async def promote_candidates(ctx: Ctx):
    _require_tag_manager(ctx)
    async with get_session_factory()() as session:
        counts = await td.visible_counts(session, ctx, ignore_blocked=True)
        free = [
            t
            for t in await td.active_tags(session)
            if t.group_id == rules.FREE and counts.get(t.id, 0) > 0
        ]
        recent: dict[int, int] = {}
        if free:
            conds = await svc.listable_conditions(session, td.ctx_ignoring_blocked(ctx))
            since = utc_now() - timedelta(days=30)
            recent = {
                int(k): int(v)
                for k, v in (
                    await session.execute(
                        select(SiteWorkTag.tag_id, func.count())
                        .join(Work, Work.id == SiteWorkTag.work_id)
                        .where(
                            SiteWorkTag.tag_id.in_([t.id for t in free]),
                            SiteWorkTag.created_at >= since,
                            *conds,
                        )
                        .group_by(SiteWorkTag.tag_id)
                    )
                ).all()
            }
    items = [
        {
            "tag": td.directory_item(t, counts),
            "recentUses": recent.get(t.id, 0),
            # 自定义榜单（/lists）还没有，出现次数先为 0；旧索引页分类的迁移是后续数据任务
            "listAppearances": 0,
            "legacyCategory": None,
        }
        for t in free
    ]
    items.sort(
        key=lambda x: -(
            x["recentUses"] * 2 + x["listAppearances"] + x["tag"]["workCount"]
        )
    )
    return {"items": items[:20]}
