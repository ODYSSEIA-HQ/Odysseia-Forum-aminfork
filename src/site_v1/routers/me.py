"""GET/PATCH /me、PATCH /me/prefs、R18 资格（协议 3.1，10-25 第 5 条）。"""

from fastapi import APIRouter, Body

from models.site_account import SiteRestrictedAccess, SiteUserPrefs
from shared.time_utils import utc_now
from site_v1.config import get_config
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import me_dto, restricted_access_dto
from site_v1.errors import invalid, restricted_access_required, unavailable
from site_v1.fluxer import FluxerUnavailable, get_fluxer
from site_v1.services.accounts import access_conditions, load_viewer, unmet_ids

router = APIRouter(prefix="/me", tags=["账号"])

PINNED_BOARDS_MAX = 30
TAG_LIST_MAX = 500


async def _reload(ctx) -> None:
    ctx.viewer = await load_viewer(get_session_factory(), ctx.viewer.user_id)
    if ctx.viewer is None:
        raise unavailable("账号资料暂时不可用")
    ctx.refresh_mode()


@router.get("", summary="自己的资料和偏好")
async def get_me(viewer: CurrentUser):
    return me_dto(viewer)


@router.patch("", summary="改自己的资料")
async def patch_me(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    display_name = body.get("displayName")
    if display_name is not None:
        if (
            not isinstance(display_name, str)
            or not display_name.strip()
            or len(display_name.strip()) > 32
        ):
            raise invalid({"displayName": "昵称 1-32 个字"})
        try:
            await get_fluxer().update_profile(
                ctx.fluxer_token or "",
                viewer.user_id,
                display_name=display_name.strip(),
            )
        except FluxerUnavailable as exc:
            raise unavailable("资料服务暂时不可用") from exc
        await _reload(ctx)
    return me_dto(ctx.viewer)


def _string_list(value, *, limit: int) -> list[str] | None:
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v for v in value
    ):
        return None
    unique = list(dict.fromkeys(value))
    return unique if len(unique) <= limit else None


@router.patch("/prefs", summary="改偏好；切换全年龄 / R18 模式")
async def patch_prefs(viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)):
    fields: dict[str, str] = {}
    changes: dict = {}
    if "contentMode" in body:
        if body["contentMode"] not in ("general", "r18"):
            fields["contentMode"] = "内容模式只能是 general 或 r18"
        else:
            changes["content_mode"] = body["contentMode"]
    for key, column in (("showR18G", "show_r18g"), ("blurR18", "blur_r18")):
        if key in body:
            if not isinstance(body[key], bool):
                fields[key] = "只能是开或关"
            else:
                changes[column] = body[key]
    for key, column in (
        ("blockedTags", "blocked_tag_ids"),
        ("followedTags", "followed_tag_ids"),
    ):
        if key in body:
            value = _string_list(body[key], limit=TAG_LIST_MAX)
            if value is None:
                fields[key] = f"要是 tag ID 列表，最多 {TAG_LIST_MAX} 个"
            else:
                changes[column] = value
    if "dmPrivacy" in body:
        if body["dmPrivacy"] not in ("everyone", "friends", "nobody"):
            fields["dmPrivacy"] = "私信隐私取值不对"
        else:
            changes["dm_privacy"] = body["dmPrivacy"]
    if "pinnedBoards" in body:
        value = _string_list(body["pinnedBoards"], limit=PINNED_BOARDS_MAX)
        if value is None:
            fields["pinnedBoards"] = (
                f"钉选的榜单要是榜单 ID 列表，最多 {PINNED_BOARDS_MAX} 个"
            )
        else:
            changes["pinned_boards"] = value
    if fields:
        raise invalid(fields)

    if changes.get("content_mode") == "r18":
        conditions = await access_conditions(viewer)
        unmet = unmet_ids(viewer, conditions)
        if viewer.access.state != "granted" or unmet:
            # unmet_ids 保证非空：撤销时含 revoked，未授予时至少有一个条件 ID
            raise restricted_access_required(unmet)

    if changes:
        async with get_session_factory()() as session:
            row = await session.get(SiteUserPrefs, viewer.user_id, with_for_update=True)
            for column, value in changes.items():
                setattr(row, column, value)
            row.updated_at = utc_now()
            await session.commit()
        # 切回全年龄后立即按 general 过滤（下一个请求从库里现读，不缓存）
        await _reload(ctx)
    return me_dto(ctx.viewer)


@router.get("/restricted-access", summary="R18 资格和不满足的条件")
async def get_restricted_access(viewer: CurrentUser):
    return {
        "restrictedAccess": restricted_access_dto(viewer),
        "termsVersion": get_config().terms_version,
        "conditions": await access_conditions(viewer),
    }


@router.post("/restricted-access/confirm", summary="确认满 18 岁和条款版本")
async def confirm_restricted_access(
    viewer: CurrentUser, ctx: Ctx, body: dict = Body(...)
):
    config = get_config()
    if body.get("termsVersion") != config.terms_version:
        raise invalid({"termsVersion": "条款已更新，请重新阅读"})
    now = utc_now()
    async with get_session_factory()() as session:
        row = await session.get(
            SiteRestrictedAccess, viewer.user_id, with_for_update=True
        )
        row.age_confirmed_at = now
        row.terms_version = config.terms_version
        row.checked_at = now
        row.updated_at = now
        await session.commit()
    await _reload(ctx)
    viewer = ctx.viewer
    # 条件都满足就授予；被撤销的资格不会因此恢复
    if viewer.access.state != "revoked":
        conditions = await access_conditions(viewer, config)
        ok = all(c["met"] for c in conditions)
        async with get_session_factory()() as session:
            row = await session.get(
                SiteRestrictedAccess, viewer.user_id, with_for_update=True
            )
            row.state = "granted" if ok else "none"
            row.source = "terms_confirm" if ok else None
            row.updated_at = utc_now()
            await session.commit()
        if not ok:
            async with get_session_factory()() as session:
                prefs = await session.get(
                    SiteUserPrefs, viewer.user_id, with_for_update=True
                )
                prefs.content_mode = "general"
                await session.commit()
        await _reload(ctx)
    return me_dto(ctx.viewer)
