"""协议 3.2 作品：列表、详情、建/改/删/发布、收藏、加删 tag；
以及第二期里能用现有数据做最简实现的：/works/random、/works/{id}/similar。

实现在 site_v1/services/works.py（可见性、查询、序列化、分级同步），规则在 tag_rules.py。
"""

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select

from models.site_tag import SiteTag, SiteWorkTag
from models.work import Work
from shared.time_utils import utc_now
from site_v1.config import get_config
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.errors import SiteError, forbidden, invalid, not_found, unauthenticated
from site_v1.paging import PageRequest, pagination
from site_v1.services import tag_rules as rules
from site_v1.services import works as svc
from site_v1.services.attachments import load_attachments

router = APIRouter(tags=["作品"])

PATCH_FORBIDDEN_KEYS = (
    "rating",
    "tags",
    "tagIds",
    "visibility",
    "declarations",
    "status",
)


def _strip_control(value: str) -> str:
    """去掉控制字符（协议 1.3），保留换行和制表符。"""
    return "".join(ch for ch in value if ord(ch) >= 32 or ch in "\t\n\r")


def _check_text(fields, key, value, max_len, required=False):
    if value is None:
        return
    if not isinstance(value, str):
        fields[key] = "格式不对"
    elif required and not value.strip():
        fields[key] = "不能为空"
    elif len(value.strip()) > max_len:
        fields[key] = f"最长 {max_len} 个字"


def _sort(raw: str | None, allowed=svc.WORK_SORTS, default="hot_week") -> str:
    if raw is None or raw == "":
        return default
    if raw not in allowed:
        raise invalid({"sort": f"sort 只能是 {', '.join(allowed)}"})
    return raw


def list_filters(request: Request) -> dict:
    """/works、/boards/{id}、/works/random 共用的 tags / excludeTags / type / rating 参数。"""
    qp = request.query_params
    ratings = svc.parse_csv(qp.getlist("rating"))
    bad = [r for r in ratings if r not in ("general", "r18", "r18g")]
    if bad:
        raise invalid({"rating": "rating 只能是 general、r18、r18g"})
    return {
        "tags": svc.parse_csv(qp.getlist("tags")),
        "exclude_tags": svc.parse_csv(qp.getlist("excludeTags")),
        "type_name": qp.get("type") or None,
        "ratings": ratings or None,
        "tag_logic": "or" if qp.get("tagLogic") == "or" else "and",
    }


# ---------------- 列表 ----------------


@router.get("/works", summary="作品列表")
async def list_works(
    ctx: Ctx,
    sort: str | None = None,
    page: PageRequest = Depends(pagination),
    filters: dict = Depends(list_filters),
):
    sort = _sort(sort)
    filters.pop("tag_logic")
    async with get_session_factory()() as session:
        conds = await svc.listable_conditions(session, ctx, **filters)
        return await svc.query_page(session, ctx, conds, page, sort)


@router.get("/works/random", summary="随机抽卡（第二期，最简实现）")
async def random_works(
    ctx: Ctx,
    limit: str | None = None,
    scope: str | None = None,
    filters: dict = Depends(list_filters),
):
    try:
        size = min(max(int(limit or 10), 1), 50)
    except ValueError:
        size = 10
    async with get_session_factory()() as session:
        conds = await svc.listable_conditions(session, ctx, **filters)
        if scope == "followed_tags":
            if ctx.viewer is None:
                raise unauthenticated()
            followed = [svc.parse_id(t) for t in ctx.viewer.prefs.followed_tags]
            followed = [t for t in followed if t is not None]
            if not followed:
                return {"items": []}
            conds.append(svc.has_any_tag(followed))
        works = (
            (
                await session.execute(
                    select(Work).where(*conds).order_by(func.random()).limit(size)
                )
            )
            .scalars()
            .all()
        )
        return {"items": await svc.serialize_works(session, ctx, list(works))}


# ---------------- 详情 ----------------


@router.get("/works/{id}", summary="作品详情")
async def get_work(id: str, ctx: Ctx):
    async with get_session_factory()() as session:
        work = await svc.get_visible_work(session, ctx, id)
        return await svc.serialize_work(session, ctx, work)


@router.get("/works/{id}/similar", summary="相似作品（第二期，按共有 tag 的最简实现）")
async def similar_works(id: str, ctx: Ctx, limit: str | None = None):
    try:
        size = min(max(int(limit or 6), 1), 24)
    except ValueError:
        size = 6
    async with get_session_factory()() as session:
        source = await svc.get_visible_work(session, ctx, id)
        tag_ids = [r.tag_id for r in await svc.active_tag_rows(session, source.id)]
        # 分级 tag 不算相似
        if tag_ids:
            tag_ids = list(
                (
                    await session.execute(
                        select(SiteTag.id).where(
                            SiteTag.id.in_(tag_ids), SiteTag.group_id != "rating"
                        )
                    )
                ).scalars()
            )
        if not tag_ids:
            return {"items": []}
        conds = await svc.listable_conditions(session, ctx)
        shared = (
            select(func.count())
            .select_from(SiteWorkTag)
            .where(
                SiteWorkTag.work_id == Work.id,
                SiteWorkTag.removed_at.is_(None),
                SiteWorkTag.tag_id.in_(tag_ids),
            )
            .scalar_subquery()
        )
        works = (
            (
                await session.execute(
                    select(Work)
                    .where(*conds, Work.id != source.id, shared > 0)
                    .order_by(shared.desc(), Work.id.desc())
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )
        return {"items": await svc.serialize_works(session, ctx, list(works))}


# ---------------- 建、改、删 ----------------


@router.post("/works", summary="建草稿", status_code=201)
async def create_work(viewer: CurrentUser, ctx: Ctx, body: dict | None = Body(None)):
    body = body if isinstance(body, dict) else {}
    title = body.get("title", "")
    summary = body.get("summary", "")
    fields: dict[str, str] = {}
    _check_text(fields, "title", title, rules.MAX_TITLE_LENGTH)
    _check_text(fields, "summary", summary, rules.MAX_SUMMARY_LENGTH)
    if fields:
        raise invalid(fields)
    now = utc_now()
    async with get_session_factory()() as session:
        work = Work(
            title=_strip_control(title).strip(),
            summary=_strip_control(summary).strip(),
            description="",
            images=[],
            metadata_overrides=[],
            author_id=viewer.user_id,
            # 草稿不建原生帖子：发布时才排建帖任务；source_visible=false 让 V2 列表看不到草稿
            source_platform="fluxer",
            source_visible=False,
            status="draft",
            visibility="public",
            created_at=now,
            updated_at=now,
        )
        session.add(work)
        await session.flush()
        result = await svc.serialize_work(session, ctx, work)
        await session.commit()
    return JSONResponse(result, status_code=201)


@router.patch("/works/{id}", summary="改标题、简介、说明、封面、图库、布局")
async def patch_work(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    async with get_session_factory()() as session:
        work = await svc.get_editable_work(session, ctx, id)
        fields: dict[str, str] = {}
        for key in PATCH_FORBIDDEN_KEYS:
            if key in body:
                fields[key] = "分级和 tag 请用 tags 接口修改，发布请用 publish 接口"
        _check_text(
            fields,
            "title",
            body.get("title"),
            rules.MAX_TITLE_LENGTH,
            work.status != "draft",
        )
        _check_text(fields, "summary", body.get("summary"), rules.MAX_SUMMARY_LENGTH)
        _check_text(fields, "description", body.get("description"), 20000)
        if "layout" in body:
            layout = body["layout"]
            if layout is not None and not isinstance(layout, dict):
                fields["layout"] = "布局格式不对"
            elif layout is not None and len(str(layout)) > 20000:
                fields["layout"] = "布局太大了，自定义区块的内容请精简一些"
        cover = None
        if "coverId" in body and body["coverId"] is not None:
            found = await _usable_attachment(session, body["coverId"], viewer, work)
            if isinstance(found, str):
                fields["coverId"] = found
            else:
                cover = found
        gallery = None
        if "galleryIds" in body:
            raw = body["galleryIds"]
            if not isinstance(raw, list) or len(raw) > 20:
                fields["galleryIds"] = "图库最多 20 张"
            else:
                gallery = []
                for item in raw:
                    found = await _usable_attachment(session, item, viewer, work)
                    if isinstance(found, str):
                        fields["galleryIds"] = found
                    else:
                        gallery.append(found)
        if fields:
            raise invalid(fields)
        if work.source_platform == "discord" and any(
            k in body for k in ("title", "description")
        ):
            raise forbidden("DC 迁移来的作品，标题和说明跟随来源帖子同步，不能在这里改")
        # 已发布作品的图片分级不能高于作品分级（草稿在发布时统一检查）
        if work.status != "draft":
            rating = svc.rating_of(work)
            conflicts = svc.rating_conflicts(
                rating, ([cover] if cover else []) + (gallery or [])
            )
            if conflicts:
                raise svc.rating_conflict_error(rating, conflicts)

        changed_text = {}
        if isinstance(body.get("title"), str):
            work.title = _strip_control(body["title"]).strip()
            changed_text["title"] = work.title
        if isinstance(body.get("summary"), str):
            work.summary = _strip_control(body["summary"]).strip()
        if isinstance(body.get("description"), str):
            work.description = _strip_control(body["description"])
            changed_text["description"] = work.description
        if "coverId" in body:
            work.cover_attachment_id = cover.id if cover else None
        if gallery is not None:
            work.gallery_attachment_ids = [a.id for a in gallery]
        if "layout" in body:
            work.layout = body["layout"]
        work.updated_at = utc_now()
        if changed_text and work.source_platform == "fluxer":
            await _enqueue_main_edit(session, work, viewer, changed_text)
        await session.flush()
        result = await svc.serialize_work(session, ctx, work)
        await session.commit()
    return result


async def _usable_attachment(session, raw_id, viewer, work):
    """能挂到作品上的封面/图库图片：为这个作品上传的（POST /uploads 带 workId，
    purpose=cover/gallery）、state=ready 的图片（和 attachments.usable_work_attachments 同一套条件，
    这里逐条给出更具体的错误信息）。"""
    att_id = svc.parse_id(raw_id)
    found = (await load_attachments(session, [att_id])).get(att_id) if att_id else None
    if (
        found is None
        or found.work_id != work.id
        or found.purpose not in ("cover", "gallery")
    ):
        return "找不到这个文件"
    if found.state in ("uploading", "processing"):
        return "还在上传或自动审核，等结果出来再用"
    if found.state != "ready":
        if found.reject_reason == "rating_conflict":
            return "这张图的分级高于作品分级，换图或改分级后再用"
        return "这个文件不能使用"
    if not svc.is_image(found):
        return "只能用图片"
    return found


async def _enqueue_main_edit(session, work, viewer, changes: dict):
    """已建好原生主帖的作品改标题/说明：真实环境排 V2 的 edit_thread 任务同步到 Fluxer。"""
    if get_config().fluxer_fake or not work.source_thread_id:
        return
    from models.thread import Thread
    from models.work_task import WorkTask
    import uuid

    main = (
        (
            await session.execute(
                select(Thread).where(Thread.work_id == work.id, Thread.role == "main")
            )
        )
        .scalars()
        .first()
    )
    if main is None:
        return
    payload = {
        "actor_id": viewer.user_id,
        "guild_id": str(main.guild_id),
        "thread_id": str(main.thread_id),
    }
    if "title" in changes:
        payload["title"] = changes["title"]
    if "description" in changes:
        # V2 原生正文上限 4000 字，超出部分只留在本地说明里
        payload["body"] = (changes["description"] or work.title)[:4000]
    session.add(
        WorkTask(
            work_id=work.id,
            kind="edit_thread",
            dedupe_key="main-edit:" + uuid.uuid4().hex,
            payload=payload,
        )
    )


@router.delete("/works/{id}", summary="软删除", status_code=204)
async def delete_work(id: str, viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        work_id = svc.parse_id(id)
        work = (
            await session.get(Work, work_id, with_for_update=True) if work_id else None
        )
        if work is None or work.deleted_at is not None or work.status == "removed":
            raise not_found()
        staff = viewer.has_permission("works.manage_any") or viewer.has_permission(
            "moderation.content"
        )
        if work.author_id != viewer.user_id and not staff:
            if svc.visible_to(
                ctx, work, bool(await svc.restricted_ids(session, [work.id]))
            ):
                raise forbidden("只有作者和站务可以删除作品")
            raise not_found()
        # 和 V2 的软删除一样：deleted_at + deletion_reason，原生讨论区按 V2 的锁定策略处理
        work.deleted_at = utc_now()
        work.deletion_reason = "moderation"
        work.updated_at = utc_now()
        if not get_config().fluxer_fake:
            from work.work_moderation_service import WorkModerationService

            await WorkModerationService(session).policy_tasks(
                work.id, {"id": viewer.user_id}, "forum_soft_delete", True
            )
        await session.commit()
    return Response(status_code=204)


# ---------------- 发布 ----------------


@router.post("/works/{id}/publish", summary="发布")
async def publish_work(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    config = get_config()
    async with get_session_factory()() as session:
        work = await svc.get_editable_work(session, ctx, id)
        if work.author_id != viewer.user_id and not viewer.has_permission(
            "works.manage_any"
        ):
            raise forbidden("只有作者能发布")
        if work.status != "draft":
            raise SiteError(409, "conflict", "这个作品已经发布过了")
        answers = body.get("answers", [])
        if not isinstance(answers, list) or any(
            not isinstance(a, dict) for a in answers
        ):
            raise invalid({"answers": "格式不对"})
        catalog = await rules.load_catalog(session)
        tag_ids = await svc.current_tag_ids(session, catalog, work.id)

        issues = []
        title = (work.title or "").strip()
        if not title:
            issues.append({"field": "title", "message": "请填写作品标题"})
        elif len(title) > rules.MAX_TITLE_LENGTH:
            issues.append(
                {"field": "title", "message": f"标题最长 {rules.MAX_TITLE_LENGTH} 个字"}
            )
        if len((work.summary or "").strip()) > rules.MAX_SUMMARY_LENGTH:
            issues.append(
                {
                    "field": "summary",
                    "message": f"简介最长 {rules.MAX_SUMMARY_LENGTH} 个字",
                }
            )
        answered, answer_issues = rules.apply_answers(catalog, tag_ids, answers)
        issues.extend(answer_issues)
        for issue in rules.validate_tag_ids(catalog, answered):
            issues.append({**issue, "field": "tags"})
        acknowledged = body.get("acknowledgedTermsVersion")
        if acknowledged != config.terms_version:
            issues.append(
                {
                    "field": "acknowledgedTermsVersion",
                    "code": "terms",
                    "message": "发布规则已更新，请重新阅读并确认"
                    if acknowledged
                    else "请阅读并勾选发布承诺",
                }
            )
        attachments = await svc.work_attachments(session, work)
        processing = [a for a in attachments if a.state == "processing"]
        if processing:
            issues.append(
                {
                    "field": "attachments",
                    "code": "processing",
                    "message": f"还有 {len(processing)} 个文件在自动审核，等结果出来再发布",
                }
            )
        if issues:
            raise SiteError(
                422,
                "validation_failed",
                issues[0]["message"],
                {
                    "fields": rules.issues_to_fields(issues),
                    "issues": [
                        {
                            k: v
                            for k, v in {
                                "code": i.get("code", i["field"]),
                                "message": i["message"],
                                "field": i["field"],
                                "group": i.get("group"),
                            }.items()
                            if v is not None
                        }
                        for i in issues
                    ],
                },
            )
        rating = rules.rating_from_ids(catalog, answered) or "general"
        conflicts = svc.rating_conflicts(rating, attachments)
        if conflicts:
            raise svc.rating_conflict_error(rating, conflicts)

        # 必答问题：回答「含」的自动打上限制可见 tag；回答都记进 declarations
        await svc.apply_tag_ids(session, ctx, work, catalog, tag_ids, answered)
        now = utc_now()
        from site_v1.services.versions import latest_version_row

        latest = await latest_version_row(session, work.id)
        version_id = str(latest.id) if latest else None
        at = svc.iso_utc(now)
        work.declarations = [
            *(work.declarations or []),
            *(
                {
                    "question": q.id,
                    "answer": next(
                        a["answer"] for a in answers if a.get("question") == q.id
                    ),
                    "versionId": version_id,
                    "at": at,
                }
                for q in rules.applicable_questions(catalog, tag_ids)
            ),
        ]
        work.status = "published"
        work.published_at = now
        work.updated_at = now
        if config.fluxer_fake:
            # fake 模式：建原生主帖视为同步成功
            work.source_visible = True
        else:
            svc.enqueue_create_main(session, work, viewer.user_id)
        await session.flush()
        result = await svc.serialize_work(session, ctx, work)
        await session.commit()
    return result


# ---------------- 收藏 ----------------


async def _favorite(id: str, viewer, ctx, on: bool) -> Response:
    from work.work_service import WorkService

    async with get_session_factory()() as session:
        work = await svc.get_visible_work(session, ctx, id)
        await WorkService(session).collect(
            work,
            {"id": viewer.user_id, "discord_user_id": viewer.discord_user_id},
            on,
        )
        await session.commit()
    return Response(status_code=204)


@router.post("/works/{id}/favorite", summary="收藏", status_code=204)
async def favorite(id: str, viewer: CurrentUser, ctx: Ctx):
    return await _favorite(id, viewer, ctx, True)


@router.delete("/works/{id}/favorite", summary="取消收藏", status_code=204)
async def unfavorite(id: str, viewer: CurrentUser, ctx: Ctx):
    return await _favorite(id, viewer, ctx, False)


# ---------------- 加删 tag ----------------


@router.post("/works/{id}/tags", summary="加 tag")
async def add_tag(id: str, viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    if not isinstance(body, dict):
        raise invalid({"body": "格式不对"})
    async with get_session_factory()() as session:
        work_id = svc.parse_id(id)
        work = (
            await session.get(Work, work_id, with_for_update=True) if work_id else None
        )
        if work is None or work.deleted_at is not None or work.status == "removed":
            raise not_found()
        manager = svc.can_manage(work, viewer)
        if not manager and (
            work.status != "published"
            or not svc.visible_to(
                ctx, work, bool(await svc.restricted_ids(session, [work.id]))
            )
        ):
            raise not_found()
        catalog = await rules.load_catalog(session)
        tag_id = await svc.resolve_tag_request(session, catalog, body, viewer)
        info = catalog.info(tag_id)
        if not manager:
            # 官方 tag 只有作者和管理员能加；其他人只能在已发布、没锁定的作品上加自由 tag
            if info.official:
                raise forbidden("官方 tag 只有作者和管理员能加")
            if viewer.is_newcomer:
                raise SiteError(
                    403, "onboarding_required", "新人缓冲期内不能给别人的作品加 tag"
                )
            if work.tag_locked:
                raise forbidden("作者锁定了这个作品的 tag，不能再加")
        current = await svc.current_tag_ids(session, catalog, work.id)
        if tag_id in current:
            result = await svc.serialize_work(session, ctx, work)
            await session.commit()
            return JSONResponse(result, status_code=200)
        change = rules.add_tag_id(catalog, current, tag_id)
        if not change.ok:
            raise invalid({"tags": change.error}, change.error)
        if work.status == "published":
            dropped = [x for x in current if x not in change.ids]
            if any(catalog.info(x).restricts for x in dropped) or svc.is_downgrade(
                catalog, current, change.ids
            ):
                raise svc.review_required(
                    "已发布作品降低分级或去掉限制可见的 tag，需要提交理由并经审核员确认（暂未开放）"
                )
        if work.rating_lock and rules.rating_from_ids(
            catalog, change.ids
        ) != rules.rating_from_ids(catalog, current):
            raise forbidden("站务锁定了这个作品的分级", reason="rating_locked")
        await svc.apply_tag_ids(session, ctx, work, catalog, current, change.ids)
        result = await svc.serialize_work(session, ctx, work)
        await session.commit()
    return JSONResponse(result, status_code=201)


@router.delete("/works/{id}/tags/{tagId}", summary="删 tag")
async def remove_tag(id: str, tagId: str, viewer: CurrentUser, ctx: Ctx):
    async with get_session_factory()() as session:
        work_id = svc.parse_id(id)
        work = (
            await session.get(Work, work_id, with_for_update=True) if work_id else None
        )
        if work is None or work.deleted_at is not None or work.status == "removed":
            raise not_found()
        manager = svc.can_manage(work, viewer)
        if not manager and not svc.visible_to(
            ctx, work, bool(await svc.restricted_ids(session, [work.id]))
        ):
            raise not_found()
        catalog = await rules.load_catalog(session)
        current = await svc.current_tag_ids(session, catalog, work.id)
        tag_num = svc.parse_id(tagId)
        if not manager:
            added_by = (
                await svc.added_by_of(session, work.id, tag_num) if tag_num else None
            )
            if tagId not in current or added_by != viewer.user_id:
                raise forbidden("只能删自己加的 tag；其他 tag 由作者决定")
        if tagId not in current:
            result = await svc.serialize_work(session, ctx, work)
            await session.commit()
            return result
        nxt = rules.remove_tag_id(catalog, current, tagId)
        if work.status == "published":
            info = catalog.info(tagId)
            if info and info.restricts:
                raise svc.review_required(
                    "去掉限制可见的 tag，需要提交理由并经审核员确认（暂未开放）"
                )
            if (
                info
                and info.group != rules.FREE
                and rules.group_issues(catalog, nxt, info.group)
            ):
                raise invalid({"tags": f"「{info.name}」所在的组是必选的，不能删空"})
            if svc.is_downgrade(catalog, current, nxt):
                raise svc.review_required(
                    "已发布作品降低分级需要经审核员确认（暂未开放）"
                )
        await svc.apply_tag_ids(session, ctx, work, catalog, current, nxt)
        result = await svc.serialize_work(session, ctx, work)
        await session.commit()
    return result
