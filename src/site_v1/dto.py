"""协议 1.3 数据格式工具：camelCase、ID 字符串、ISO UTC 时间、附件和用户对象。

两种写法都行：
1. 请求体 / 响应模型继承 CamelModel（字段写 snake_case，进出都用 camelCase 别名）；
2. 直接拼 dict 时键写 camelCase，或用 camelize() 转换一个 snake_case 的 dict。

不要把 V2 服务返回的 dict 原样透传出去（snake_case、时间不带 Z、带热度数字）。
"""

from datetime import datetime, timezone
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from site_v1.context import Viewer


class CamelModel(BaseModel):
    """camelCase 别名的 Pydantic 基类：model_dump(by_alias=True) 输出 camelCase。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def dump(self, **kwargs) -> dict:
        return self.model_dump(by_alias=True, **kwargs)


def camel(name: str) -> str:
    return to_camel(name)


def camelize(value: Any) -> Any:
    """递归把 dict 的 snake_case 键转成 camelCase（值不动）。"""
    if isinstance(value, dict):
        return {
            camel(k) if isinstance(k, str) else k: camelize(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [camelize(v) for v in value]
    return value


def id_str(value: Any) -> str | None:
    return None if value is None else str(value)


def iso_utc(value: datetime | None) -> str | None:
    """ISO 8601 UTC，毫秒精度，以 Z 结尾。库里的 naive 时间按 UTC 处理。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


# ---------------- 附件 ----------------


def external_image(
    attachment_id: str, url: str | None, name: str = "image", rating: str = "general"
) -> dict:
    """外部图片（Discord CDN 头像、DC 迁移图）临时包成协议 Attachment。"""
    return {
        "id": attachment_id,
        "kind": "image",
        "mime": "image/png",
        "size": 0,
        "name": name,
        "url": url,
        "placeholder": False,
        "thumbUrl": url,
        "rating": rating,
        "reviewState": "auto",
        "state": "ready",
        "rejectReason": None,
    }


def avatar_dto(user_id: str, avatar_url: str | None) -> dict | None:
    if not avatar_url:
        return None
    return external_image(f"avatar_{user_id}", avatar_url, "avatar.png")


# ---------------- 用户 ----------------


def decorations_dto() -> dict:
    """装饰（身份组外观是第二期，先给空）。"""
    return {"nameColor": None, "font": None, "frame": None, "badges": [], "title": None}


def user_dto(
    *,
    user_id: str,
    username: str,
    display_name: str | None,
    avatar_url: str | None,
    created_at: datetime | None,
    discord_linked: bool,
    is_newcomer: bool,
) -> dict:
    """协议 User（公开资料）。"""
    return {
        "id": str(user_id),
        "username": username,
        "displayName": display_name or username,
        "avatar": avatar_dto(str(user_id), avatar_url),
        "decorations": decorations_dto(),
        "createdAt": iso_utc(created_at) or "1970-01-01T00:00:00.000Z",
        "discordLinked": discord_linked,
        "isNewcomer": is_newcomer,
    }


def viewer_user_dto(viewer: Viewer) -> dict:
    return user_dto(
        user_id=viewer.user_id,
        username=viewer.username,
        display_name=viewer.display_name,
        avatar_url=viewer.avatar_url,
        created_at=viewer.created_at,
        discord_linked=viewer.discord_linked,
        is_newcomer=viewer.is_newcomer,
    )


def restricted_access_dto(viewer: Viewer) -> dict:
    a = viewer.access
    return {
        "state": a.state,
        "source": a.source,
        "checkedAt": iso_utc(a.checked_at),
        "ageConfirmedAt": iso_utc(a.age_confirmed_at),
        "termsVersion": a.terms_version,
    }


def prefs_dto(viewer: Viewer) -> dict:
    p = viewer.prefs
    return {
        "contentMode": p.content_mode,
        "showR18G": p.show_r18g,
        "blurR18": p.blur_r18,
        "blockedTags": list(p.blocked_tags),
        "followedTags": list(p.followed_tags),
        "dmPrivacy": p.dm_privacy,
        "pinnedBoards": list(p.pinned_boards),
    }


def me_dto(viewer: Viewer) -> dict:
    """协议 Me。"""
    return {
        **viewer_user_dto(viewer),
        "email": viewer.email,
        "restrictedAccess": restricted_access_dto(viewer),
        "prefs": prefs_dto(viewer),
        "onboarding": {
            "quizPassed": viewer.quiz_passed or not viewer.is_newcomer,
            "newcomerUntil": iso_utc(viewer.newcomer_until)
            if viewer.is_newcomer
            else None,
        },
        "permissions": sorted(viewer.permissions),
    }


def session_me_dto(viewer: Viewer) -> dict:
    """GET /session 的简要 me。"""
    return {
        "id": viewer.user_id,
        "username": viewer.username,
        "displayName": viewer.display_name or viewer.username,
        "avatar": avatar_dto(viewer.user_id, viewer.avatar_url),
    }


async def load_users(session, user_ids: Iterable[str]) -> dict[str, dict]:
    """批量读公开资料（作品作者、举报人等都用它）：返回 {user_id: User dto}；不存在的不在结果里。"""
    from models.discord_identity import DiscordIdentity
    from models.site_account import SiteAccount
    from models.user import User
    from shared.time_utils import utc_now

    ids = list({str(i) for i in user_ids if i})
    if not ids:
        return {}
    users = (
        (await session.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    )
    accounts = {
        a.user_id: a
        for a in (
            await session.execute(
                select(SiteAccount).where(SiteAccount.user_id.in_(ids))
            )
        ).scalars()
    }
    linked = set(
        (
            await session.execute(
                select(DiscordIdentity.user_id).where(
                    DiscordIdentity.user_id.in_(ids),
                    DiscordIdentity.is_current.is_(True),
                )
            )
        ).scalars()
    )
    now = utc_now()
    result = {}
    for u in users:
        account = accounts.get(u.id)
        result[u.id] = user_dto(
            user_id=u.id,
            username=u.username,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            created_at=account.created_at if account else u.updated_at,
            discord_linked=u.id in linked,
            is_newcomer=bool(
                account and account.newcomer_until and account.newcomer_until > now
            ),
        )
    return result
