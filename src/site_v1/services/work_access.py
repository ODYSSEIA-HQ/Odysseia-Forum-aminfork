"""上传和版本接口用的作品读取和权限判断（直接读 Work 表，不依赖作品服务）。

- 能管理（发版本、给作品传文件）：作者本人，或有 works.manage_any 的账号。
  第一期没有卡贴管理员表；作品领域加了以后改 can_manage_work 一处即可。
- 可见：未删除、status≠removed、moderation_state≠removed；草稿和被隐藏的只有能管理的人看得到；
  再按有效模式（ctx.can_view(rating, visibility)）判断，看不到一律 404，不暴露存在。
"""

from models.work import Work
from site_v1 import mode as modes
from site_v1.context import SiteContext, Viewer
from site_v1.errors import forbidden, not_found


def parse_id(raw) -> int | None:
    """协议 ID 是字符串；本路线的作品、版本、附件 ID 都是正整数。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.isdigit() and len(raw) <= 19:
        value = int(raw)
        return value if value > 0 else None
    return None


def work_rating(work: Work) -> str:
    return modes.work_rating(bool(work.is_nsfw), bool(work.is_abyss))


def can_manage_work(work: Work, viewer: Viewer | None) -> bool:
    if viewer is None:
        return False
    return work.author_id == viewer.user_id or viewer.has_permission("works.manage_any")


def work_exists(work: Work | None) -> bool:
    return (
        work is not None
        and work.deleted_at is None
        and work.status != "removed"
        and work.moderation_state != "removed"
    )


def work_visible(work: Work | None, ctx: SiteContext) -> bool:
    if not work_exists(work):
        return False
    if work.status == "draft" or work.moderation_state == "hidden":
        if not can_manage_work(work, ctx.viewer):
            return False
    return ctx.can_view(work_rating(work), work.visibility or "public")


async def load_visible_work(session, raw_id, ctx: SiteContext) -> Work:
    work_id = parse_id(raw_id)
    work = await session.get(Work, work_id) if work_id else None
    if not work_visible(work, ctx):
        raise not_found()
    return work


async def load_manageable_work(
    session, raw_id, ctx: SiteContext, viewer: Viewer, *, for_update: bool = False
) -> Work:
    """能管理的作品（草稿、受限作品在全年龄模式下也行）；不能管理：看得到 403，看不到 404。"""
    work_id = parse_id(raw_id)
    work = (
        await session.get(Work, work_id, with_for_update=for_update)
        if work_id
        else None
    )
    if not work_exists(work):
        raise not_found()
    if not can_manage_work(work, viewer):
        if work_visible(work, ctx):
            raise forbidden("只有作者和卡贴管理员可以这样做")
        raise not_found()
    return work
