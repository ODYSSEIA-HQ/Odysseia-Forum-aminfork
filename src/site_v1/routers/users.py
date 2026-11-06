"""他人资料、作品、关注和屏蔽用户（协议 3.1）。"""

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from models.site_account import SiteUserPrefs
from models.user import User
from site_v1.deps import Ctx, CurrentUser, get_session_factory
from site_v1.dto import load_users
from site_v1.errors import invalid, not_found
from site_v1.paging import PageRequest, pagination
from site_v1.services import social, user_works

router = APIRouter(prefix="/users", tags=["账号"])


async def _existing_user(session, user_id: str) -> User:
    if not user_id or len(user_id) > 64:
        raise not_found("用户不存在")
    user = await session.get(User, user_id)
    if user is None or user.merged_into_id:
        raise not_found("用户不存在")
    return user


def _dm_state(target_prefs: SiteUserPrefs | None, friends: bool) -> str:
    """和私信创建（POST /conversations）同一套规则（好友是第二期，先恒为非好友）。"""
    privacy = target_prefs.dm_privacy if target_prefs else "everyone"
    if privacy == "nobody":
        return "closed"
    if privacy == "friends" and not friends:
        return "friends_only"
    return "allowed"


@router.get("/{id}", summary="他人资料")
async def get_user(id: str, ctx: Ctx):
    async with get_session_factory()() as session:
        await _existing_user(session, id)
        profile = (await load_users(session, [id]))[id]
        viewer = ctx.viewer
        if viewer is not None and viewer.user_id != id:
            target_prefs = await session.get(SiteUserPrefs, id)
            profile["viewer"] = {
                "followed": await social.is_following(session, viewer.user_id, id),
                "blocked": await social.is_blocked(session, viewer.user_id, id),
                "friend": False,
                "friendRequest": None,
                "dm": _dm_state(target_prefs, False),
            }
    return profile


@router.get("/{id}/works", summary="他人作品（按有效模式过滤）")
async def get_user_works(
    id: str, ctx: Ctx, sort: str | None = None, page: PageRequest = Depends(pagination)
):
    if sort is not None and sort not in ("new", "latest", "favorites"):
        raise invalid({"sort": "排序只能是 new、latest、favorites"})
    async with get_session_factory()() as session:
        await _existing_user(session, id)
    return await user_works.list_user_works(ctx, id, page, sort or "new")


async def _relation(viewer, id: str, action) -> Response:
    async with get_session_factory()() as session:
        await _existing_user(session, id)
        if id == viewer.user_id:
            raise invalid({"userId": "不能对自己这样做"})
        await action(session)
        await session.commit()
    return Response(status_code=204)


@router.post("/{id}/follow", summary="关注用户", status_code=204)
async def follow_user(id: str, viewer: CurrentUser):
    return await _relation(
        viewer, id, lambda s: social.set_follow(s, viewer.user_id, id, True)
    )


@router.delete("/{id}/follow", summary="取消关注用户", status_code=204)
async def unfollow_user(id: str, viewer: CurrentUser):
    return await _relation(
        viewer, id, lambda s: social.set_follow(s, viewer.user_id, id, False)
    )


@router.post("/{id}/block", summary="屏蔽用户", status_code=204)
async def block_user(id: str, viewer: CurrentUser):
    return await _relation(
        viewer, id, lambda s: social.set_block(s, viewer.user_id, id, True)
    )


@router.delete("/{id}/block", summary="取消屏蔽用户", status_code=204)
async def unblock_user(id: str, viewer: CurrentUser):
    return await _relation(
        viewer, id, lambda s: social.set_block(s, viewer.user_id, id, False)
    )
