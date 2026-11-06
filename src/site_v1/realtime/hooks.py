"""在「现有服务产生变化处」发布外层事件：挂在 SQLAlchemy Session 上的 ORM 钩子。

不用在每个路由里加调用：
- after_flush：从 session.new / session.dirty 里认出关心的变化，只记 ID 和变化（不持有 ORM 对象）；
- after_commit：事务提交后把收集到的变化交给后台任务（publisher.spawn），用新的数据库会话现读、
  按收件人现在的有效模式过滤后发布；
- 回滚时丢掉。
没配 CENTRIFUGO_API_URL / CENTRIFUGO_API_KEY 时 after_flush 直接返回，什么也不收集。

| 变化 | 事件 | 频道 |
| --- | --- | --- |
| 新 site_notification / notification（V2，有 fluxer_user_id） | notification.created + unread.updated(scope=notifications, id=分类) | personal:{收件人}；来源作品对收件人当前模式不可见时不推 |
| 新 site_announcement（已到发布时间） | announcement.published | audience=all → site，r18 → site_r18 |
| work 的标题/简介/封面/状态/分级…；新版本、作品公告、tag、规则、钉选 | work.updated {workId, fields} | work:{id} |
| work.visibility 变了 | work.visibility.changed；转为受限 / 下架时把看不到的订阅者踢出 | work:{id} |
| site_restricted_access.state、site_user_prefs.content_mode / show_r18g 变了 | access.changed，然后断开本人连接（3014） | personal:{用户} |
| 新 work_user_mute / user_moderation_record(kind=mute) | moderation.muted | personal:{被禁言的人} |
| attachment.state 变成 ready / rejected，或分级变了 | upload.updated（附件对象） | personal:{上传者} |
"""

import logging
from collections import OrderedDict

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import get_history

from site_v1.realtime import channels, publisher
from site_v1.realtime.config import publishing_enabled

logger = logging.getLogger(__name__)

INFO_KEY = "site_v1_realtime"

# work 列 → work.updated 的 fields（协议 Work 字段名）；不在这里的列（updated_at、热度……）不推
WORK_FIELDS = {
    "title": "title",
    "summary": "summary",
    "description": "description",
    "cover_attachment_id": "cover",
    "gallery_attachment_ids": "gallery",
    "layout": "layout",
    "status": "status",
    "published_at": "status",
    "deleted_at": "status",
    "moderation_state": "status",
    "source_visible": "status",
    "is_nsfw": "rating",
    "is_abyss": "rating",
    "rating_lock": "rating",
    "visibility": "visibility",
    "tag_locked": "tags",
    "declarations": "declarations",
}
# 这些列变了要检查订阅者是否还看得到（下架、删除）
KICK_FIELDS = {"status", "deleted_at", "moderation_state", "source_visible", "is_abyss"}

_installed = False


def _changed(obj, attr: str) -> tuple[bool, object, object]:
    """(是否真的变了, 旧值, 新值)。旧值没加载过时按变了处理。"""
    hist = get_history(obj, attr)
    if not hist.added:
        return False, None, None
    new = hist.added[0]
    old = hist.deleted[0] if hist.deleted else None
    if hist.deleted and old == new:
        return False, old, new
    return True, old, new


def _collect(session: Session) -> list[tuple]:
    from models.attachment import Attachment
    from models.notification import Notification
    from models.site_account import SiteRestrictedAccess, SiteUserPrefs
    from models.site_me import (
        SiteAnnouncement,
        SiteNotification,
        SiteWorkPin,
        SiteWorkRules,
    )
    from models.site_moderation import UserModerationRecord
    from models.site_tag import SiteWorkTag
    from models.work import Work
    from models.work_announcement import WorkAnnouncement
    from models.work_user_mute import WorkUserMute
    from models.work_version import WorkVersion
    from site_v1.dto import iso_utc

    out: list[tuple] = []
    for obj in session.new:
        if isinstance(obj, SiteNotification):
            out.append(("notification", "n", obj.id, obj.user_id))
        elif isinstance(obj, Notification):
            if obj.fluxer_user_id:
                out.append(("notification", "v", obj.id, obj.fluxer_user_id))
        elif isinstance(obj, SiteAnnouncement):
            out.append(("announcement", obj.id))
        elif isinstance(obj, WorkVersion):
            out.append(
                ("work", obj.work_id, ("versions", "latestVersion"), None, False)
            )
        elif isinstance(obj, WorkAnnouncement):
            out.append(("work", obj.work_id, ("announcements",), None, False))
        elif isinstance(obj, SiteWorkTag):
            out.append(("work", obj.work_id, ("tags",), None, False))
        elif isinstance(obj, SiteWorkRules):
            out.append(("work", obj.work_id, ("rules",), None, False))
        elif isinstance(obj, SiteWorkPin):
            out.append(("work", obj.work_id, ("pins",), None, False))
        elif isinstance(obj, WorkUserMute):
            data = {
                "scope": "work",
                "workId": str(obj.work_id),
                "until": iso_utc(obj.expires_at),
            }
            out.append(("muted", obj.user_id, data))
        elif isinstance(obj, UserModerationRecord):
            if obj.kind == "mute":
                out.append(
                    (
                        "muted",
                        obj.user_id,
                        {"scope": "site", "until": iso_utc(obj.until)},
                    )
                )
    for obj in session.dirty:
        if not session.is_modified(obj, include_collections=False):
            continue
        if isinstance(obj, Work):
            fields: set[str] = set()
            visibility = None
            kick = False
            for column, name in WORK_FIELDS.items():
                changed, _old, new = _changed(obj, column)
                if not changed:
                    continue
                fields.add(name)
                if column == "visibility":
                    visibility = new
                if column in KICK_FIELDS:
                    kick = True
            if fields:
                out.append(("work", obj.id, tuple(sorted(fields)), visibility, kick))
        elif isinstance(obj, SiteWorkTag):
            out.append(("work", obj.work_id, ("tags",), None, False))
        elif isinstance(obj, (SiteWorkRules, SiteWorkPin)):
            name = "rules" if isinstance(obj, SiteWorkRules) else "pins"
            out.append(("work", obj.work_id, (name,), None, False))
        elif isinstance(obj, SiteRestrictedAccess):
            if _changed(obj, "state")[0]:
                out.append(("access", obj.user_id))
        elif isinstance(obj, SiteUserPrefs):
            if _changed(obj, "content_mode")[0] or _changed(obj, "show_r18g")[0]:
                out.append(("access", obj.user_id))
        elif isinstance(obj, Attachment):
            state_changed, _, new_state = _changed(obj, "state")
            rating_changed = _changed(obj, "rating")[0]
            state = new_state if state_changed else obj.state
            if (state_changed or rating_changed) and state in ("ready", "rejected"):
                out.append(("upload", obj.id, obj.owner_id))
    return out


def _after_flush(session, flush_context) -> None:
    if not publishing_enabled():
        return
    try:
        items = _collect(session)
    except Exception:  # noqa: BLE001 — 收集失败不影响提交
        logger.exception("收集实时事件失败")
        return
    if items:
        session.info.setdefault(INFO_KEY, []).extend(items)


def _after_commit(session) -> None:
    items = session.info.pop(INFO_KEY, None)
    if items:
        publisher.spawn(dispatch(items))


def _after_rollback(session, *args) -> None:
    session.info.pop(INFO_KEY, None)


def install() -> None:
    """注册到所有 Session（AsyncSession 底下的同步 Session 也是它的子类）。只注册一次。"""
    global _installed
    if _installed:
        return
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_soft_rollback", _after_rollback)
    _installed = True


# ---------------- 提交之后：现读、过滤、发布 ----------------


def _merge(items: list[tuple]) -> list[tuple]:
    """同一事务里同一作品的多次变化合成一条 work.updated；其余去重、保持顺序。"""
    works: "OrderedDict[int, dict]" = OrderedDict()
    others: "OrderedDict[tuple, tuple]" = OrderedDict()
    for item in items:
        if item[0] == "work":
            _, work_id, fields, visibility, kick = item
            if work_id is None:
                continue
            w = works.setdefault(
                int(work_id), {"fields": set(), "visibility": None, "kick": False}
            )
            w["fields"].update(fields)
            if visibility is not None:
                w["visibility"] = visibility
            w["kick"] = w["kick"] or kick
        else:
            key = tuple(
                tuple(sorted(x.items())) if isinstance(x, dict) else x for x in item
            )
            if key not in others:
                others[key] = item
    merged = [
        ("work", wid, sorted(w["fields"]), w["visibility"], w["kick"])
        for wid, w in works.items()
    ]
    return merged + list(others.values())


async def dispatch(items: list[tuple]) -> None:
    for item in _merge(items):
        try:
            kind = item[0]
            if kind == "notification":
                await _notification(item[1], item[2], item[3])
            elif kind == "announcement":
                await _announcement(item[1])
            elif kind == "work":
                await _work(item[1], item[2], item[3], item[4])
            elif kind == "access":
                await _access(item[1])
            elif kind == "muted":
                from site_v1.realtime.events import outer

                await outer(channels.personal(item[1]), "moderation.muted", item[2])
            elif kind == "upload":
                await _upload(item[1], item[2])
        except Exception:  # noqa: BLE001
            logger.exception("发布实时事件失败：%s", item[:2])


async def _notification(src: str, notification_id: int, user_id: str) -> None:
    from sqlalchemy import select

    from site_v1.deps import get_session_factory
    from site_v1.realtime.events import outer, viewer_ctx
    from site_v1.services import notifications as notes

    if notification_id is None or not user_id:
        return
    ctx = await viewer_ctx(user_id)
    if ctx is None:
        return
    async with get_session_factory()() as session:
        u, conds = await notes.union_query(session, ctx)
        row = (
            await session.execute(
                select(u).where(*conds, u.c.src == src, u.c.id == notification_id)
            )
        ).first()
        if row is None:
            # 来源作品对收件人现在的模式不可见（受限作品 + 全年龄模式等）：不推
            return
        item = (await notes._hydrate(session, [row]))[0]
        counts = await notes.unread_counts(session, ctx)
    channel = channels.personal(user_id)
    await outer(channel, "notification.created", item)
    await outer(
        channel,
        "unread.updated",
        {
            "scope": "notifications",
            "id": row.category,
            "count": int(counts["categories"].get(row.category, 0)),
        },
    )


async def _announcement(announcement_id: int) -> None:
    from models.site_me import SiteAnnouncement
    from shared.time_utils import utc_now
    from site_v1.deps import get_session_factory
    from site_v1.realtime.events import outer
    from site_v1.routers.notifications import announcement_dto

    async with get_session_factory()() as session:
        row = await session.get(SiteAnnouncement, announcement_id)
        if row is None or row.deleted_at is not None:
            return
        if row.published_at is not None and row.published_at > utc_now():
            return  # 定时发布的公告：到时间不会补推（见 README「实时推送」没做到的）
        data = announcement_dto(row)
        audience = row.audience
    channel = channels.SITE if audience == "all" else channels.SITE_R18
    await outer(channel, "announcement.published", data)


async def _work(work_id: int, fields: list[str], visibility, kick: bool) -> None:
    from site_v1.realtime.events import kick_invisible, outer

    channel = channels.work(work_id)
    if fields:
        await outer(
            channel, "work.updated", {"workId": str(work_id), "fields": list(fields)}
        )
    if visibility in ("public", "restricted"):
        await outer(
            channel,
            "work.visibility.changed",
            {"workId": str(work_id), "visibility": visibility},
        )
    if visibility == "restricted" or kick:
        await kick_invisible(work_id)


async def _access(user_id: str) -> None:
    from site_v1.dto import restricted_access_dto
    from site_v1.realtime.events import outer, viewer_ctx

    ctx = await viewer_ctx(user_id)
    if ctx is None:
        return
    await outer(
        channels.personal(user_id),
        "access.changed",
        {"restrictedAccess": restricted_access_dto(ctx.viewer)},
    )
    # 服务端必须主动断开不再允许的订阅：断开连接，客户端用新令牌重连（site_r18、受限作品重新校验）
    await publisher.disconnect(user_id)


async def _upload(attachment_id: int, owner_id: str) -> None:
    from models.attachment import Attachment
    from site_v1.deps import get_session_factory
    from site_v1.realtime.events import outer, viewer_ctx
    from site_v1.services.attachments import attachment_dto

    ctx = await viewer_ctx(owner_id)
    if ctx is None:
        return
    async with get_session_factory()() as session:
        att = await session.get(Attachment, attachment_id)
        if att is None:
            return
        data = attachment_dto(att, ctx)
    await outer(channels.personal(owner_id), "upload.updated", data)
