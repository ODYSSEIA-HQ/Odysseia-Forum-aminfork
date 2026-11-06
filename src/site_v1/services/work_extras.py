"""作品内：公告、确认（needsAck）、规则、钉选、关注、作品内禁言、DC 原帖存档（协议 3.2、3.19）。

复用 V2 的事实：公告 work_announcement（发公告时按 V2 同样方式给关注者写 notification）、
关注 thread_follow、作品内禁言 work_user_mute（V2 WorkModerationService.mute）。
协议多出来的确认、规则、钉选、存档在 site_work_ack / site_work_rules / site_work_pin / site_dc_archive_post。
"""

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert

from models.notification import Notification
from models.site_me import SiteNotification, SiteWorkAck, SiteWorkRules
from models.thread import Thread
from models.thread_follow import ThreadFollow
from models.work import Work
from models.work_announcement import WorkAnnouncement
from shared.time_utils import utc_now
from site_v1.services.work_access import can_manage_work

ACK_KINDS = ("announcement", "rules", "version")


# ---------------- 公告序号 ----------------


def announcement_seq():
    """作品内递增序号（按发布时间、ID 排的行号；撤回的也占号，序号不变）。"""
    return (
        func.row_number()
        .over(
            partition_by=WorkAnnouncement.work_id,
            order_by=(WorkAnnouncement.created_at, WorkAnnouncement.id),
        )
        .label("seq")
    )


async def latest_announcement_seq(session, work_ids) -> dict[int, int]:
    """每个作品最新一条（没撤回的）公告的序号。"""
    ids = list(work_ids)
    if not ids:
        return {}
    numbered = (
        select(
            WorkAnnouncement.work_id,
            WorkAnnouncement.withdrawn_at,
            announcement_seq(),
        )
        .where(WorkAnnouncement.work_id.in_(ids))
        .subquery()
    )
    rows = (
        await session.execute(
            select(numbered.c.work_id, func.max(numbered.c.seq))
            .where(numbered.c.withdrawn_at.is_(None))
            .group_by(numbered.c.work_id)
        )
    ).all()
    return {int(w): int(s) for w, s in rows}


async def rules_versions(session, work_ids) -> dict[int, SiteWorkRules]:
    ids = list(work_ids)
    if not ids:
        return {}
    return {
        r.work_id: r
        for r in (
            await session.execute(
                select(SiteWorkRules).where(SiteWorkRules.work_id.in_(ids))
            )
        ).scalars()
    }


# ---------------- needsAck ----------------


async def needs_ack_map(
    session, viewer, works: list[Work], latest_version_ids: dict[int, str | None]
) -> dict[int, dict]:
    """批量算 Work.viewer.needsAck：最新公告序号 / 规则版本 / 最新版本 ID 和本人确认记录比对。

    没登录、能管理这个作品（作者、管理员）一律 false；没有公告、规则正文为空、没有版本的那一项为 false。
    """
    result = {
        w.id: {"announcement": False, "rules": False, "version": False} for w in works
    }
    if viewer is None:
        return result
    ids = [w.id for w in works if not can_manage_work(w, viewer)]
    if not ids:
        return result
    seqs = await latest_announcement_seq(session, ids)
    rules = await rules_versions(session, ids)
    acks: dict[tuple[int, str], str] = {
        (a.work_id, a.kind): a.version
        for a in (
            await session.execute(
                select(SiteWorkAck).where(
                    SiteWorkAck.user_id == viewer.user_id, SiteWorkAck.work_id.in_(ids)
                )
            )
        ).scalars()
    }
    for work_id in ids:
        state = result[work_id]
        seq = seqs.get(work_id)
        if seq is not None:
            state["announcement"] = acks.get((work_id, "announcement")) != str(seq)
        r = rules.get(work_id)
        if r is not None and r.body.strip():
            state["rules"] = acks.get((work_id, "rules")) != str(r.version)
        latest = latest_version_ids.get(work_id)
        if latest:
            state["version"] = acks.get((work_id, "version")) != str(latest)
    return result


async def set_ack(session, user_id: str, work_id: int, kind: str, version: str) -> None:
    stmt = insert(SiteWorkAck).values(
        user_id=user_id, work_id=work_id, kind=kind, version=version, acked_at=utc_now()
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id", "work_id", "kind"],
            set_={"version": stmt.excluded.version, "acked_at": stmt.excluded.acked_at},
        )
    )


# ---------------- 关注者提醒 ----------------


def follower_ids_query(work_id: int, exclude_user_id: str | None):
    q = select(ThreadFollow.fluxer_user_id).where(
        ThreadFollow.work_id == work_id,
        ThreadFollow.active_flag.is_(True),
        ThreadFollow.fluxer_user_id.is_not(None),
    )
    if exclude_user_id:
        q = q.where(ThreadFollow.fluxer_user_id != exclude_user_id)
    return q


async def notify_announcement_followers(
    session,
    work_id: int,
    row: WorkAnnouncement,
    actor_id: str,
    actor_discord: str | None,
) -> None:
    """和 V2 WorkAnnouncementService.publish_announcement 写同样的 notification 行
    （event_type=work_announcement），只是发布者可以没有 Discord 绑定。"""
    discord_thread = (
        select(Thread.thread_id)
        .where(
            Thread.work_id == work_id,
            Thread.platform == "discord",
            Thread.deleted_at.is_(None),
            Thread.show_flag.is_(True),
        )
        .order_by(Thread.id)
        .limit(1)
        .scalar_subquery()
    )
    conds = [
        or_(
            ThreadFollow.work_id == work_id,
            and_(
                ThreadFollow.work_id.is_(None),
                ThreadFollow.thread_id.in_(
                    select(Thread.thread_id).where(
                        Thread.work_id == work_id, Thread.platform == "discord"
                    )
                ),
            ),
        ),
        ThreadFollow.active_flag.is_(True),
        ThreadFollow.followed_at <= row.created_at,
        or_(
            ThreadFollow.fluxer_user_id.is_(None),
            ThreadFollow.fluxer_user_id != actor_id,
        ),
    ]
    if actor_discord and str(actor_discord).isdigit():
        conds.append(
            or_(
                ThreadFollow.user_id.is_(None),
                ThreadFollow.user_id != int(actor_discord),
            )
        )
    recipients = select(
        ThreadFollow.user_id,
        ThreadFollow.fluxer_user_id,
        literal("work_announcement"),
        literal(row.id),
        literal(work_id),
        literal(row.created_at),
        literal("forum"),
        discord_thread,
    ).where(*conds)
    await session.execute(
        insert(Notification)
        .from_select(
            [
                "user_id",
                "fluxer_user_id",
                "event_type",
                "event_source_id",
                "work_id",
                "created_at",
                "event_source_platform",
                "thread_id",
            ],
            recipients,
        )
        .on_conflict_do_nothing()
    )


async def notify_work_update(session, work: Work, actor_id: str, excerpt: str) -> None:
    """关注作品的人收到 work_update（规则更新等 V2 没有的事件，写 site_notification）。"""
    ids = (await session.execute(follower_ids_query(work.id, actor_id))).scalars().all()
    for user_id in set(ids):
        session.add(
            SiteNotification(
                user_id=user_id,
                category="work_update",
                actor_id=actor_id,
                target={"type": "work", "id": str(work.id), "title": work.title},
                excerpt=excerpt,
                work_id=work.id,
            )
        )


# ---------------- 关注 ----------------


async def set_work_follow(session, work: Work, viewer, follow: bool) -> None:
    """和 V2 WorkFollowService.follow 写同一份 thread_follow；没有 Discord 绑定时 user_id 留空。"""
    row = (
        (
            await session.execute(
                select(ThreadFollow)
                .where(
                    ThreadFollow.fluxer_user_id == viewer.user_id,
                    ThreadFollow.work_id == work.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    now = utc_now()
    if row is None:
        if not follow:
            return
        main = (
            (
                await session.execute(
                    select(Thread).where(
                        Thread.work_id == work.id,
                        Thread.role == "main",
                        Thread.platform == "discord",
                    )
                )
            )
            .scalars()
            .first()
        )
        discord = viewer.discord_user_id
        session.add(
            ThreadFollow(
                fluxer_user_id=viewer.user_id,
                user_id=int(discord) if discord and str(discord).isdigit() else None,
                work_id=work.id,
                thread_id=main.thread_id if main else None,
                followed_at=now,
                last_viewed_at=now,
                active_flag=True,
            )
        )
        return
    if follow and not row.active_flag:
        row.active_flag = True
        row.followed_at = now
        row.last_viewed_at = now
    elif not follow:
        row.active_flag = False
