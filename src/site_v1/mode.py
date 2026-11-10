"""协议 1.1 有效模式、分级映射和可见性（3.12）。

- 有效模式 r18 当且仅当：已登录、prefs.contentMode=r18、restrictedAccess.state=granted；
  每个请求现算，不缓存，撤销和切换下一个请求立即生效。
- 分级：Work.is_abyss → r18g，is_nsfw → r18，否则 general。
- r18g 只在有效模式 r18 且 prefs.showR18G 时可见（/api/v1 不看 V2 的 Discord 深渊身份组）。
"""

from typing import Iterable

GENERAL = "general"
R18 = "r18"
R18G = "r18g"
RATINGS = (GENERAL, R18, R18G)
_RANK = {GENERAL: 0, R18: 1, R18G: 2}


def effective_mode(
    authenticated: bool, content_mode: str | None, access_state: str | None
) -> str:
    """协议 1.1 的有效模式。"""
    if authenticated and content_mode == R18 and access_state == "granted":
        return R18
    return GENERAL


def work_rating(is_nsfw: bool, is_abyss: bool) -> str:
    """V2 两个布尔值 → 协议 rating。"""
    if is_abyss:
        return R18G
    if is_nsfw:
        return R18
    return GENERAL


def rating_flags(rating: str) -> tuple[bool, bool]:
    """协议 rating → (is_nsfw, is_abyss)。r18g 两个都置真，和 V2 的「深渊 NSFW」分区对应。"""
    if rating == R18G:
        return True, True
    if rating == R18:
        return True, False
    return False, False


def rating_rank(rating: str | None) -> int:
    return _RANK.get(rating or GENERAL, 0)


def max_rating(ratings: Iterable[str | None]) -> str:
    best = GENERAL
    for r in ratings:
        if rating_rank(r) > rating_rank(best):
            best = r  # type: ignore[assignment]
    return best


def visible_ratings(mode: str, show_r18g: bool) -> set[str]:
    """当前查看者能看到的分级集合（列表过滤、详情 404 判断都用它）。"""
    if mode != R18:
        return {GENERAL}
    return {GENERAL, R18, R18G} if show_r18g else {GENERAL, R18}


def narrow_ratings(
    requested: Iterable[str] | None, mode: str, show_r18g: bool
) -> set[str]:
    """列表的 rating 参数只能收窄：和可见集合取交集；不带参数时就是可见集合。"""
    allowed = visible_ratings(mode, show_r18g)
    if not requested:
        return allowed
    return allowed & {r for r in requested if r in RATINGS}


def work_visibility(rating: str, has_restricting_tag: bool) -> str:
    """restricted 的条件：分级不是 general，或绑了任一 restricts=true 的 tag（如「破限」）。"""
    return "restricted" if rating != GENERAL or has_restricting_tag else "public"


def can_view(mode: str, show_r18g: bool, rating: str, visibility: str) -> bool:
    """作品/附件对当前查看者是否可见。restricted 只对有效模式 r18 可见。"""
    if visibility == "restricted" and mode != R18:
        return False
    return rating in visible_ratings(mode, show_r18g)
