"""作品的可见性和管理权限：/api/v1 全部领域（作品、tag、版本、上传、聊天外层、举报、审核）
只用这一份判断。这里只依赖模型和 mode，不 import 其它 services，避免循环引用。

可见（3.12，对齐假后端 workVisibleTo）：
- 软删除（deleted_at 有值）、status=removed、moderation_state=removed 的作品对谁都不存在
  （审核下架 = status=removed 且 deleted_at 有值）；
- 已上线（published / archived / unclaimed）、没被隐藏、来源可见的作品对所有人开放；
  草稿、被隐藏的、原生帖还没投影出来的只有能管理的人看得到；
- 再按有效模式：分级由 is_nsfw/is_abyss 得出，restricted = 分级不是 general 或绑了
  restricts=true 的 tag（现算，不依赖 work.visibility 冗余列，V2 改分级不会同步那一列）；
  r18g 只在有效模式 r18 且 showR18G 时可见（ctx.can_view）。
- 看不到一律 404，不暴露存在。列表的 SQL 条件见 services/works.listable_conditions，语义同上
  （列表只给 status=published），另外过滤屏蔽的 tag 和用户。

能管理（发版本、改作品、给作品传文件）：作者本人，或有 works.manage_any 的账号。
第一期没有卡贴管理员表；以后加了只改 can_manage_work。
"""

from sqlalchemy import select

from models.site_tag import SiteTag, SiteWorkTag
from models.work import Work
from site_v1 import mode as modes
from site_v1.context import SiteContext, Viewer
from site_v1.errors import forbidden, not_found

LIVE_STATUSES = ("published", "archived", "unclaimed")
HIDDEN_MODERATION = ("hidden", "removed")


def parse_id(raw) -> int | None:
    """协议 ID 是字符串；本路线的作品、版本、附件、tag ID 都是正整数（bigint）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if 0 < raw < 2**63 else None
    if isinstance(raw, str) and raw.isdigit() and len(raw) <= 19:
        value = int(raw)
        return value if 0 < value < 2**63 else None
    return None


def work_rating(work: Work) -> str:
    return modes.work_rating(bool(work.is_nsfw), bool(work.is_abyss))


def can_manage_work(work: Work, viewer: Viewer | None) -> bool:
    if viewer is None:
        return False
    return work.author_id == viewer.user_id or viewer.has_permission("works.manage_any")


def work_exists(work: Work | None) -> bool:
    """作品还在（没被删、没被下架）；不看查看者。"""
    return (
        work is not None
        and work.deleted_at is None
        and work.status != "removed"
        and work.moderation_state != "removed"
    )


def source_ok(work: Work) -> bool:
    """来源帖可见；还没建好原生主帖的 Fluxer 作品也算（可见性只看本地状态）。"""
    return bool(work.source_visible) or (
        work.source_platform == "fluxer" and not work.source_thread_id
    )


def is_open_to_all(work: Work) -> bool:
    """作品本身对所有人开放（不看查看者和分级）。"""
    return (
        work_exists(work)
        and work.status in LIVE_STATUSES
        and work.moderation_state not in HIDDEN_MODERATION
        and source_ok(work)
    )


async def restricted_ids(session, work_ids) -> set[int]:
    """绑了 restricts=true 的 tag 的作品 ID。"""
    ids = list({int(i) for i in work_ids if i})
    if not ids:
        return set()
    rows = await session.execute(
        select(SiteWorkTag.work_id)
        .join(SiteTag, SiteTag.id == SiteWorkTag.tag_id)
        .where(
            SiteWorkTag.work_id.in_(ids),
            SiteWorkTag.removed_at.is_(None),
            SiteTag.restricts.is_(True),
            SiteTag.deleted_at.is_(None),
        )
        .distinct()
    )
    return set(rows.scalars())


def visible_to(ctx: SiteContext, work: Work | None, restricted_tag: bool) -> bool:
    """同步版本：restricted_tag 由调用方批量查好（restricted_ids）。"""
    if not work_exists(work):
        return False
    if not is_open_to_all(work) and not can_manage_work(work, ctx.viewer):
        return False
    rating = work_rating(work)
    return ctx.can_view(rating, modes.work_visibility(rating, restricted_tag))


async def work_visible(session, ctx: SiteContext, work: Work | None) -> bool:
    if not work_exists(work):
        return False
    return visible_to(ctx, work, bool(await restricted_ids(session, [work.id])))


async def get_visible_work(session, ctx: SiteContext, raw_id, *, for_update=False) -> Work:
    work_id = parse_id(raw_id)
    work = (
        await session.get(Work, work_id, with_for_update=for_update or None)
        if work_id
        else None
    )
    if not await work_visible(session, ctx, work):
        raise not_found()
    return work


async def get_editable_work(
    session, ctx: SiteContext, raw_id, *, for_update: bool = True
) -> Work:
    """能管理的作品（草稿、受限作品在全年龄模式下也行）；不能管理：看得到 403，看不到 404。"""
    work_id = parse_id(raw_id)
    work = (
        await session.get(Work, work_id, with_for_update=for_update or None)
        if work_id
        else None
    )
    if not work_exists(work):
        raise not_found()
    if not can_manage_work(work, ctx.viewer):
        if await work_visible(session, ctx, work):
            raise forbidden("只有作者和卡贴管理员可以这样做")
        raise not_found()
    return work


async def load_visible_work(session, raw_id, ctx: SiteContext) -> Work:
    """get_visible_work 的旧参数顺序（上传、版本路由在用）。"""
    return await get_visible_work(session, ctx, raw_id)


async def load_manageable_work(
    session, raw_id, ctx: SiteContext, viewer: Viewer, *, for_update: bool = False
) -> Work:
    return await get_editable_work(session, ctx, raw_id, for_update=for_update)
