from types import SimpleNamespace
import logging
from datetime import timedelta

from sqlalchemy import select, text

from core.tag_data_repository import TagDataRepository
from models import Notification, Thread
from models.tag_notification_task import TagNotificationTask
from models.tag_proposal import TagProposal
from identity.runtime import is_fluxer_mode
from identity.business_identity import resolve_business_id
from shared.tag_error import TagError
from shared.time_utils import utc_now
from tag.custom_tag_service import CustomTagService

logger = logging.getLogger(__name__)


class TagWorker:
    """恢复可重试通知任务并处理到期的标签申请。"""

    def __init__(self, session_factory, config):
        self.session_factory = session_factory
        self.config = config

    async def expire(self):
        """按提交期限处理申请，锁定目标后复查状态。"""
        async with self.session_factory() as session:
            ids = list(
                (
                    await session.execute(
                        select(TagProposal.id)
                        .where(
                            TagProposal.status == "pending",
                            TagProposal.due_at <= utc_now(),
                        )
                        .order_by(TagProposal.due_at, TagProposal.id)
                        .limit(100)
                    )
                ).scalars()
            )
        for proposal_id in ids:
            try:
                async with self.session_factory() as session, session.begin():
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock_shared(73902141)")
                    )
                    proposal = await session.get(TagProposal, proposal_id)
                    if proposal is None:
                        continue
                    service = CustomTagService(session, self.config)
                    try:
                        kind, tid, target, _ = await service.target(
                            proposal.applicant_id,
                            {
                                "target_type": proposal.target_type,
                                "target_id": proposal.target_id,
                                "_internal_target": True,
                            },
                        )
                        await session.refresh(proposal)
                        if proposal.status != "pending" or proposal.due_at > utc_now():
                            continue
                        await service.resolve(
                            0, proposal, kind, tid, target, True, automatic=True
                        )
                    except TagError as exc:
                        if exc.status >= 500:
                            raise
                        # 已失去可见性的申请也应结束，而不是永久等待。
                        await session.refresh(proposal, with_for_update=True)
                        if proposal.status == "pending":
                            proposal.status, proposal.reason, proposal.resolved_at = (
                                "failed",
                                exc.detail["code"],
                                utc_now(),
                            )
                            await service.log(
                                "tag.review",
                                None,
                                proposal.target_type,
                                proposal.target_id,
                                proposal.tag_id,
                                proposal_id=str(proposal.id),
                                status="failed",
                                reason=proposal.reason,
                            )
            except Exception:
                logger.exception(
                    "标签超时处理失败，稍后重试 proposal_id=%s", proposal_id
                )

    async def deliver(self, send, send_lifecycle=None):
        """领取一条通知，发送失败保留重试，永久失败站内兜底。"""
        async with self.session_factory() as session, session.begin():
            statement = (
                select(TagNotificationTask)
                .where(
                    TagNotificationTask.status == "pending",
                    TagNotificationTask.available_at <= utc_now(),
                )
                .order_by(TagNotificationTask.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            task = (await session.execute(statement)).scalar_one_or_none()
            if task is None:
                return
            if task.kind != "proposal":
                if send_lifecycle is None:
                    return
                try:
                    await send_lifecycle(task)
                    task.status = "sent"
                except Exception as exc:
                    task.attempts += 1
                    task.error = type(exc).__name__
                    task.available_at = utc_now() + timedelta(
                        seconds=min(3600, 30 * 2 ** min(task.attempts, 7))
                    )
                return
            proposal = await session.get(TagProposal, task.proposal_id)
            if proposal is None or proposal.status != "pending":
                task.status = "cancelled"
                return
            external_id = proposal.target_id
            if proposal.target_type == "thread":
                target = await session.get(Thread, proposal.target_id)
                if target is None:
                    task.status = "cancelled"
                    return
                external_id = target.thread_id
            notice = SimpleNamespace(
                id=proposal.id,
                owner_id=proposal.owner_id,
                target_type=proposal.target_type,
                target_id=external_id,
            )
            task.attempts += 1
            try:
                if is_fluxer_mode() and not proposal.fluxer_owner_id:
                    raise ValueError("标签申请目标作者尚未映射")
                can_send_discord = bool(proposal.owner_id)
                if is_fluxer_mode() and can_send_discord:
                    can_send_discord = (
                        await resolve_business_id(session, proposal.owner_id)
                        == proposal.fluxer_owner_id
                    )
                delivered = await send(notice) if can_send_discord else False
                if not delivered:
                    existing = await TagDataRepository(session, Notification).one(
                        (
                            Notification.fluxer_user_id
                            if is_fluxer_mode()
                            else Notification.user_id
                        )
                        == (
                            proposal.fluxer_owner_id
                            if is_fluxer_mode()
                            else proposal.owner_id
                        ),
                        Notification.event_type == "tag_review",
                        Notification.event_source_id == proposal.id,
                    )
                    if existing is None:
                        await TagDataRepository(session, Notification).add(
                            user_id=proposal.owner_id,
                            fluxer_user_id=proposal.fluxer_owner_id
                            if is_fluxer_mode()
                            else None,
                            event_type="tag_review",
                            event_source_id=proposal.id,
                            target_type=proposal.target_type,
                            target_id=external_id,
                        )
                    task.error = "Discord 通知无法送达，已使用站内通知"
                task.status = "sent"
            except Exception as exc:
                task.error = type(exc).__name__
                task.available_at = utc_now() + timedelta(
                    seconds=min(3600, 30 * 2 ** min(task.attempts, 7))
                )
                logger.warning(
                    "标签通知稍后重试 task_id=%s error=%s", task.id, task.error
                )
