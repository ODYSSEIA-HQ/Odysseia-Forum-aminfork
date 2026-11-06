"""「我的」：跟账号走的设置、浏览习惯、我的作品、收藏、关注的作品、关注的作者、作者页精选、
勋章墙、酒馆插件令牌（协议 3.1、草案 F；协议没写的按假后端）。"""

import hashlib
import json
import secrets

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, func, select, update

from models.author_follow import AuthorFollow
from models.booklist import Booklist
from models.booklist_item import BooklistItem
from models.site_me import (
    SitePinnedWorks,
    SitePluginToken,
    SiteTagHabits,
    SiteUserDecorationPref,
    SiteUserPreferencesDoc,
    SiteWorkRules,
)
from models.thread_follow import ThreadFollow
from models.work import Work
from models.work_announcement import WorkAnnouncement
from models.work_version import WorkVersion
from shared.time_utils import utc_now
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import iso_utc, load_users
from site_v1.errors import SiteError, conflict, invalid, not_found
from site_v1.paging import PageRequest, page_response, pagination
from site_v1.services import me_extra as mx
from site_v1.services.versions import latest_versions
from site_v1.services.works import listable_conditions, serialize_works

router = APIRouter(prefix="/me", tags=["我的"])

PREFERENCES_MAX_BYTES = 64 * 1024
TAG_HABITS_MAX_ENTRIES = 200
TAG_HABITS_MAX_HIDDEN = 200
PLUGIN_TOKEN_MAX = 5
PLUGIN_TOKEN_NAME_MAX = 40
PLUGIN_TOKEN_PREFIX = "odp_"
PLUGIN_DEFAULT_SCOPES = ["works:read", "library"]


# ---------------- 设置（带版本号） ----------------


def _prefs_doc(row: SiteUserPreferencesDoc | None) -> dict:
    if row is None:
        return {"version": 0, "updatedAt": None, "data": {}}
    return {
        "version": row.version,
        "updatedAt": iso_utc(row.updated_at),
        "data": row.data or {},
    }


@router.get("/preferences", summary="跟账号走的前端设置")
async def get_preferences(viewer: CurrentUser):
    async with get_session_factory()() as session:
        return _prefs_doc(await session.get(SiteUserPreferencesDoc, viewer.user_id))


@router.put("/preferences", summary="保存设置；baseVersion 和服务器不一致 409")
async def put_preferences(viewer: CurrentUser, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    fields = {}
    base = body.get("baseVersion")
    if isinstance(base, bool) or not isinstance(base, int) or base < 0:
        fields["baseVersion"] = "缺少 baseVersion"
    data = body.get("data")
    if not isinstance(data, dict) or any(
        v is not None and not isinstance(v, dict) for v in data.values()
    ):
        fields["data"] = "偏好的每个分区都要是对象"
    elif len(json.dumps(data, ensure_ascii=False).encode()) > PREFERENCES_MAX_BYTES:
        fields["data"] = f"偏好超过 {PREFERENCES_MAX_BYTES // 1024} KB"
    if fields:
        raise invalid(fields)
    async with get_session_factory()() as session:
        row = await session.get(
            SiteUserPreferencesDoc, viewer.user_id, with_for_update=True
        )
        current = row.version if row else 0
        if base != current:
            raise conflict("设置已在别的设备上改过", current=_prefs_doc(row))
        if row is None:
            row = SiteUserPreferencesDoc(user_id=viewer.user_id)
            session.add(row)
        row.version = current + 1
        row.data = {k: v for k, v in data.items() if v is not None}
        row.updated_at = utc_now()
        await session.flush()
        result = _prefs_doc(row)
        await session.commit()
    return result


# ---------------- 浏览习惯 ----------------


def _habits_doc(row: SiteTagHabits | None) -> dict:
    if row is None:
        return {"entries": [], "hidden": [], "updatedAt": None}
    return {
        "entries": row.entries or [],
        "hidden": row.hidden or [],
        "updatedAt": iso_utc(row.updated_at),
    }


@router.get("/tag-habits", summary="浏览习惯")
async def get_tag_habits(viewer: CurrentUser):
    async with get_session_factory()() as session:
        return _habits_doc(await session.get(SiteTagHabits, viewer.user_id))


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@router.put("/tag-habits", summary="整份上传浏览习惯")
async def put_tag_habits(viewer: CurrentUser, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    fields = {}
    entries = body.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(e, dict)
        or not isinstance(e.get("name"), str)
        or not _is_number(e.get("score"))
        or not isinstance(e.get("updatedAt"), str)
        for e in entries
    ):
        fields["entries"] = "习惯记录格式不对"
    elif len(entries) > TAG_HABITS_MAX_ENTRIES:
        fields["entries"] = f"最多 {TAG_HABITS_MAX_ENTRIES} 个 tag"
    hidden = body.get("hidden")
    if not isinstance(hidden, list) or any(not isinstance(h, str) for h in hidden):
        fields["hidden"] = "「不感兴趣」要是 tag 名列表"
    elif len(hidden) > TAG_HABITS_MAX_HIDDEN:
        fields["hidden"] = f"最多 {TAG_HABITS_MAX_HIDDEN} 个"
    if fields:
        raise invalid(fields)
    clean = [
        {
            "tagId": e.get("tagId") if isinstance(e.get("tagId"), str) else None,
            "name": e["name"][:100],
            "restricts": e.get("restricts")
            if isinstance(e.get("restricts"), bool)
            else None,
            "score": e["score"],
            "updatedAt": e["updatedAt"][:40],
        }
        for e in entries
    ]
    async with get_session_factory()() as session:
        row = await session.get(SiteTagHabits, viewer.user_id, with_for_update=True)
        if row is None:
            row = SiteTagHabits(user_id=viewer.user_id)
            session.add(row)
        row.entries = clean
        row.hidden = list(dict.fromkeys(h[:100] for h in hidden))
        row.updated_at = utc_now()
        await session.flush()
        result = _habits_doc(row)
        await session.commit()
    return result


@router.delete("/tag-habits", summary="清除服务器上的浏览习惯", status_code=204)
async def delete_tag_habits(viewer: CurrentUser):
    async with get_session_factory()() as session:
        await session.execute(
            delete(SiteTagHabits).where(SiteTagHabits.user_id == viewer.user_id)
        )
        await session.commit()
    return Response(status_code=204)


# ---------------- 我的作品 ----------------


@router.get("/works", summary="我的作品（published 含归档；draft 是草稿）")
async def my_works(
    viewer: CurrentUser,
    ctx: Ctx,
    status: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    status = status or "published"
    if status not in ("published", "draft"):
        raise invalid({"status": "只能是 published 或 draft"})
    conds = [
        Work.author_id == viewer.user_id,
        Work.deleted_at.is_(None),
        Work.status != "removed",
        Work.status == "draft" if status == "draft" else Work.status != "draft",
    ]
    async with get_session_factory()() as session:
        works = (
            (
                await session.execute(
                    select(Work)
                    .where(*conds)
                    .order_by(
                        func.coalesce(Work.updated_at, Work.created_at).desc(),
                        Work.id.desc(),
                    )
                    .offset(page.offset)
                    .limit(page.limit + 1)
                )
            )
            .scalars()
            .all()
        )
        items = await serialize_works(session, ctx, list(works[: page.limit]))
    result = page_response(works, page, lambda w: None)
    result["items"] = items
    return result


# ---------------- 收藏、关注的作品 ----------------


@router.get("/favorites", summary="我收藏的作品（只给当前模式能看到的）")
async def my_favorites(
    viewer: CurrentUser,
    ctx: Ctx,
    sort: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    async with get_session_factory()() as session:
        order = (
            [Work.updated_at.desc().nulls_last(), BooklistItem.id.desc()]
            if sort == "updated"
            else [BooklistItem.created_at.desc(), BooklistItem.id.desc()]
        )
        pairs = (
            await session.execute(
                select(BooklistItem.created_at, Work)
                .join(Booklist, Booklist.id == BooklistItem.booklist_id)
                .join(Work, Work.id == BooklistItem.work_id)
                .where(
                    Booklist.fluxer_owner_id == viewer.user_id,
                    Booklist.is_default.is_(True),
                    *(await listable_conditions(session, ctx)),
                )
                .order_by(*order)
                .offset(page.offset)
                .limit(page.limit + 1)
            )
        ).all()
        shown = pairs[: page.limit]
        works = await serialize_works(session, ctx, [w for _, w in shown])
    result = page_response(pairs, page, lambda p: None)
    result["items"] = [
        {"work": w, "favoritedAt": iso_utc(at)} for (at, _), w in zip(shown, works)
    ]
    return result


def _last_change():
    """作品最近一次「更新」：新版本、新公告、规则变化（草案 F）。"""
    version_at = (
        select(func.max(WorkVersion.created_at))
        .where(WorkVersion.work_id == Work.id)
        .correlate(Work)
        .scalar_subquery()
    )
    announcement_at = (
        select(func.max(WorkAnnouncement.created_at))
        .where(
            WorkAnnouncement.work_id == Work.id, WorkAnnouncement.withdrawn_at.is_(None)
        )
        .correlate(Work)
        .scalar_subquery()
    )
    rules_at = (
        select(SiteWorkRules.updated_at)
        .where(SiteWorkRules.work_id == Work.id)
        .correlate(Work)
        .scalar_subquery()
    )
    return func.greatest(version_at, announcement_at, rules_at, Work.created_at)


@router.get("/follows", summary="关注的作品（有更新的在前）")
async def my_follows(
    viewer: CurrentUser,
    ctx: Ctx,
    onlyUpdated: str | None = Query(None),
    page: PageRequest = Depends(pagination),
):
    last = _last_change().label("last_change")
    viewed = func.coalesce(ThreadFollow.last_viewed_at, ThreadFollow.followed_at)
    has_update = (last > viewed).label("has_update")
    async with get_session_factory()() as session:
        q = (
            select(
                Work,
                ThreadFollow.followed_at,
                ThreadFollow.last_viewed_at,
                viewed,
                has_update,
                last,
            )
            .join(ThreadFollow, ThreadFollow.work_id == Work.id)
            .where(
                ThreadFollow.fluxer_user_id == viewer.user_id,
                ThreadFollow.active_flag.is_(True),
                *(await listable_conditions(session, ctx)),
            )
        )
        if onlyUpdated in ("1", "true"):
            q = q.where(_last_change() > viewed)
        rows = (
            await session.execute(
                q.order_by(has_update.desc(), last.desc(), Work.id.desc())
                .offset(page.offset)
                .limit(page.limit + 1)
            )
        ).all()
        shown = rows[: page.limit]
        works = await serialize_works(session, ctx, [r[0] for r in shown])
        versions = await latest_versions(session, [r[0].id for r in shown], ctx)
    result = page_response(rows, page, lambda r: None)
    result["items"] = [
        {
            "work": w,
            "followedAt": iso_utc(r[1]),
            "lastViewedAt": iso_utc(r[3]),
            "hasUpdate": bool(r[4]),
            "latestVersion": versions.get(r[0].id),
        }
        for r, w in zip(shown, works)
    ]
    return result


@router.post("/follows/mark-viewed", summary="关注的作品标记已看", status_code=204)
async def mark_follows_viewed(
    viewer: CurrentUser, ctx: Ctx, body: dict | None = Body(None)
):
    body = body or {}
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    async with get_session_factory()() as session:
        listable = select(Work.id).where(*(await listable_conditions(session, ctx)))
        conds = [
            ThreadFollow.fluxer_user_id == viewer.user_id,
            ThreadFollow.active_flag.is_(True),
            ThreadFollow.work_id.in_(listable),
        ]
        if "workIds" in body:
            ids = body.get("workIds")
            if not isinstance(ids, list):
                raise invalid({"workIds": "格式不对"})
            wanted = [int(i) for i in ids if isinstance(i, str) and i.isdigit()]
            conds.append(ThreadFollow.work_id.in_(wanted or [-1]))
        await session.execute(
            update(ThreadFollow).where(*conds).values(last_viewed_at=utc_now())
        )
        await session.commit()
    return Response(status_code=204)


# ---------------- 关注的作者 ----------------


@router.get("/following", summary="关注的作者")
async def my_following(
    viewer: CurrentUser, ctx: Ctx, page: PageRequest = Depends(pagination)
):
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AuthorFollow.fluxer_author_id, AuthorFollow.followed_at)
                .where(
                    AuthorFollow.fluxer_user_id == viewer.user_id,
                    AuthorFollow.active_flag.is_(True),
                    AuthorFollow.fluxer_author_id.is_not(None),
                )
                .order_by(AuthorFollow.followed_at.desc(), AuthorFollow.id.desc())
                .offset(page.offset)
                .limit(page.limit + 1)
            )
        ).all()
        shown = rows[: page.limit]
        users = await load_users(session, [r[0] for r in shown])
        conds = await listable_conditions(session, ctx)
        latest: dict[str, dict | None] = {}
        for author_id, _ in shown:
            work = (
                (
                    await session.execute(
                        select(Work)
                        .where(Work.author_id == author_id, *conds)
                        .order_by(
                            func.coalesce(Work.published_at, Work.created_at).desc(),
                            Work.id.desc(),
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            latest[author_id] = (
                (await serialize_works(session, ctx, [work]))[0] if work else None
            )
    result = page_response(rows, page, lambda r: None)
    result["items"] = [
        {"user": users[a], "followedAt": iso_utc(at), "latestWork": latest.get(a)}
        for a, at in shown
        if a in users
    ]
    return result


# ---------------- 作者页精选 ----------------


@router.put("/pinned-works", summary="设置作者页精选（最多 3 件自己已发布的作品）")
async def put_pinned_works(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise invalid({"items": "要是精选作品列表"})
    if len(items) > mx.PINNED_WORKS_MAX:
        raise invalid({"items": f"最多精选 {mx.PINNED_WORKS_MAX} 件作品"})
    async with get_session_factory()() as session:
        fields = {}
        seen = set()
        clean = []
        for i, it in enumerate(items):
            work_id = it.get("workId") if isinstance(it, dict) else None
            note = it.get("note") if isinstance(it, dict) else None
            note = note.strip() if isinstance(note, str) else ""
            work = (
                await session.get(Work, int(work_id))
                if isinstance(work_id, str) and work_id.isdigit()
                else None
            )
            if (
                work is None
                or work.author_id != viewer.user_id
                or work.status != "published"
                or work.deleted_at is not None
            ):
                fields[f"items.{i}.workId"] = "只能精选自己已发布的作品"
            elif work_id in seen:
                fields[f"items.{i}.workId"] = "同一件作品不能精选两次"
            if len(note) > mx.PINNED_WORK_NOTE_MAX:
                fields[f"items.{i}.note"] = f"一句话最多 {mx.PINNED_WORK_NOTE_MAX} 字"
            seen.add(work_id)
            clean.append({"workId": work_id, "note": note})
        if fields:
            raise invalid(fields)
        row = await session.get(SitePinnedWorks, viewer.user_id, with_for_update=True)
        if row is None:
            row = SitePinnedWorks(user_id=viewer.user_id)
            session.add(row)
        row.items = clean
        row.updated_at = utc_now()
        await session.flush()
        result = await mx.pinned_works_of(session, ctx, viewer.user_id)
        await session.commit()
    return result


# ---------------- 勋章墙 ----------------


@router.get("/decorations", summary="我的勋章墙")
async def get_decorations(viewer: CurrentUser):
    async with get_session_factory()() as session:
        return await mx.my_decorations(session, viewer.user_id)


@router.patch(
    "/decorations", summary="改勋章墙：展示的勋章、名字旁不显示、各槽位用哪个组"
)
async def patch_decorations(viewer: CurrentUser, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    async with get_session_factory()() as session:
        mine = await mx.my_decorations(session, viewer.user_id)
        owned = {r["id"]: r for r in mine["decorationRoles"]}
        fields = {}
        showcase = None
        if "showcase" in body:
            raw = body["showcase"]
            if not isinstance(raw, list) or any(not isinstance(i, str) for i in raw):
                fields["showcase"] = "展示的勋章要是勋章 id 列表"
            else:
                showcase = list(dict.fromkeys(raw))
                if len(showcase) > mx.DECORATION_SHOWCASE_MAX:
                    fields["showcase"] = f"最多展示 {mx.DECORATION_SHOWCASE_MAX} 个勋章"
                elif any(i not in owned for i in showcase):
                    fields["showcase"] = "只能展示自己拥有的勋章"
        if "showcaseHidden" in body and not isinstance(body["showcaseHidden"], bool):
            fields["showcaseHidden"] = "要是 true 或 false"
        equipped = body.get("equipped")
        if equipped is not None:
            if not isinstance(equipped, dict):
                fields["equipped"] = "槽位选择格式不对"
            else:
                for slot, rid in equipped.items():
                    if slot not in mx.DECORATION_SLOTS:
                        fields[f"equipped.{slot}"] = "没有这个装饰槽位"
                    elif rid is not None and (
                        rid not in owned or slot not in (owned[rid].get("slots") or [])
                    ):
                        fields[f"equipped.{slot}"] = (
                            "只能选自己拥有、并且提供这项外观的身份组"
                        )
        if fields:
            raise invalid(fields)
        pref = await session.get(
            SiteUserDecorationPref, viewer.user_id, with_for_update=True
        )
        if pref is None:
            pref = SiteUserDecorationPref(user_id=viewer.user_id)
            session.add(pref)
        if showcase is not None:
            pref.showcase = showcase
        if "showcaseHidden" in body:
            pref.showcase_hidden = body["showcaseHidden"]
        if isinstance(equipped, dict):
            pref.equipped = {**(pref.equipped or {}), **equipped}
        await session.flush()
        result = await mx.my_decorations(session, viewer.user_id)
        await session.commit()
    return result


# ---------------- 酒馆插件令牌 ----------------


def _token_dto(t: SitePluginToken) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "createdAt": iso_utc(t.created_at),
        "lastUsedAt": iso_utc(t.last_used_at),
        "scopes": list(t.scopes or []),
        "hint": t.hint,
    }


@router.get("/plugin-tokens", summary="我的插件令牌（不含令牌本身）")
async def list_plugin_tokens(viewer: CurrentUser):
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(SitePluginToken)
                    .where(SitePluginToken.user_id == viewer.user_id)
                    .order_by(
                        SitePluginToken.created_at.desc(), SitePluginToken.id.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
    return [_token_dto(t) for t in rows]


@router.post(
    "/plugin-tokens", summary="建插件令牌；明文只在这里返回一次", status_code=201
)
async def create_plugin_token(viewer: CurrentUser, body: dict = Body(...)):
    name = body.get("name") if isinstance(body, dict) else None
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise invalid({"name": "给令牌起个名字，方便以后认出来"})
    if len(name) > PLUGIN_TOKEN_NAME_MAX:
        raise invalid({"name": f"名字最多 {PLUGIN_TOKEN_NAME_MAX} 个字"})
    async with get_session_factory()() as session:
        count = (
            await session.execute(
                select(func.count()).where(SitePluginToken.user_id == viewer.user_id)
            )
        ).scalar_one()
        if count >= PLUGIN_TOKEN_MAX:
            raise SiteError(
                409, "conflict", f"最多 {PLUGIN_TOKEN_MAX} 个令牌，先删掉不用的再建"
            )
        secret = PLUGIN_TOKEN_PREFIX + secrets.token_hex(24)
        token = SitePluginToken(
            user_id=viewer.user_id,
            name=name,
            token_hash=hashlib.sha256(secret.encode()).hexdigest(),
            hint=secret[-4:],
            scopes=list(PLUGIN_DEFAULT_SCOPES),
        )
        session.add(token)
        await session.flush()
        result = {"token": _token_dto(token), "secret": secret}
        await session.commit()
    return JSONResponse(status_code=201, content=result)


@router.delete(
    "/plugin-tokens/{id}", summary="删除插件令牌（立即失效）", status_code=204
)
async def delete_plugin_token(id: str, viewer: CurrentUser):
    async with get_session_factory()() as session:
        token = await session.get(SitePluginToken, int(id)) if id.isdigit() else None
        if token is None or token.user_id != viewer.user_id:
            raise not_found()
        await session.delete(token)
        await session.commit()
    return Response(status_code=204)
