"""Banner申请和管理服务"""

from identity.runtime import is_fluxer_mode, uses_canonical_storage
from identity.business_identity import resolve_business_id

import logging
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from banner.channel_sync import ChannelSyncService
from banner.banner_limits import (
    GLOBAL_MAX_BANNERS,
    CHANNEL_MAX_BANNERS,
    BANNER_DURATION_DAYS,
)
from banner.dto.application_result import ApplicationResult
from banner.dto.banner_approval_result import BannerApprovalResult
from banner.dto.banner_review_application import BannerReviewApplication
from banner.dto.banner_scope_status import BannerScopeStatus
from banner.dto.banner_status_item import BannerStatusItem
from banner.dto.delete_banner_result import DeleteBannerResult
from core.banner_application_repository import BannerApplicationRepository
from core.banner_carousel_repository import BannerCarouselRepository
from core.banner_waitlist_repository import BannerWaitlistRepository
from core.thread_repository import ThreadRepository
from dto.thread_dto import ThreadDTO
from models import (
    BannerApplication,
    BannerCarousel,
    Channel,
)
from shared.enum import ApplicationStatus, TargetType
from shared.time_utils import utc_now

if TYPE_CHECKING:
    from bot_main import MyBot

logger = logging.getLogger(__name__)


class BannerService:
    """Banner申请和轮播管理服务"""

    # 全频道最多3个banner
    GLOBAL_MAX_BANNERS = GLOBAL_MAX_BANNERS
    # 每个频道最多8个banner
    CHANNEL_MAX_BANNERS = CHANNEL_MAX_BANNERS
    # Banner展示时长：3天
    BANNER_DURATION_DAYS = BANNER_DURATION_DAYS
    APPLICANT_LIMIT_MESSAGE = (
        "您已有正在播放或等待展示的 Banner，暂时无法申请。请在展示结束或移除后重试。"
    )

    def __init__(
        self,
        session: AsyncSession,
        channel_sync: Optional[ChannelSyncService] = None,
    ):
        self.session = session
        self.channel_sync = channel_sync
        self.app_repo = BannerApplicationRepository(session)
        self.carousel_repo = BannerCarouselRepository(session)
        self.waitlist_repo = BannerWaitlistRepository(session)

    async def get_status(
        self, configured_channel_ids: list[int], now: datetime
    ) -> list[BannerScopeStatus]:
        """批量生成全频道及各频道的管理状态快照。"""
        # 两次查询获取完整轮播和等待数量，并在会话内转换 DTO。
        banners = await self.carousel_repo.get_all_active_for_status(now)
        waiting_counts = await self.waitlist_repo.get_counts_by_scope()
        grouped: dict[int | None, list[BannerStatusItem]] = {}
        for banner in banners:
            grouped.setdefault(banner.channel_id, []).append(
                BannerStatusItem(
                    target_id=banner.work_id
                    if banner.target_type == TargetType.WORK.value
                    else banner.thread_id,
                    target_type=banner.target_type,
                    title=banner.title,
                    end_time=banner.end_time,
                )
            )

        # 配置频道保留顺序，补充仍有记录的未配置频道。
        configured_ids = list(dict.fromkeys(configured_channel_ids))
        configured_id_set = set(configured_ids)
        recorded_ids = set(grouped) | set(waiting_counts)
        extra_ids = sorted(
            cid
            for cid in recorded_ids
            if cid is not None and cid not in configured_id_set
        )
        return [
            BannerScopeStatus(
                channel_id=cid,
                capacity=self.GLOBAL_MAX_BANNERS
                if cid is None
                else self.CHANNEL_MAX_BANNERS,
                waiting_count=waiting_counts.get(cid, 0),
                items=grouped.get(cid, []),
            )
            for cid in [None, *configured_ids, *extra_ids]
        ]

    async def has_ongoing_banner(self, applicant_id: int) -> bool:
        """跨所有展示范围检查申请人的新轮播及等待项。"""
        owner = (
            str(await resolve_business_id(self.session, applicant_id, source="discord"))
            if uses_canonical_storage()
            else None
        )
        # 同一 SQL 快照避免等待项晋升时两次查询之间出现占位空隙。
        result = await self.session.execute(
            select(
                or_(
                    self.carousel_repo.owned_banner_query(
                        applicant_id, utc_now(), owner
                    ).exists(),
                    self.waitlist_repo.owned_banner_query(applicant_id, owner).exists(),
                )
            )
        )
        return bool(result.scalar_one())

    async def get_review_snapshot(
        self, application_id: int
    ) -> BannerReviewApplication | None:
        """获取最新申请状态的独立快照。"""
        application = await self.app_repo.get_by_id(application_id)
        return (
            BannerReviewApplication.from_application(application)
            if application
            else None
        )

    async def _get_pending_for_review(self, application_id: int) -> BannerApplication:
        """按申请人加锁后重新读取，拒绝重复或过期审核操作。"""
        application = await self.app_repo.get_by_id(application_id)
        if application is None:
            raise ValueError("申请不存在")
        applicant_id = application.applicant_id
        await self.app_repo.lock_applicant(applicant_id)
        # populate_existing 强制刷新锁等待期间可能被其他事务改变的状态。
        application = await self.app_repo.get_by_id(application_id)
        if application is None:
            raise ValueError("申请不存在")
        if application.status != ApplicationStatus.PENDING.value:
            raise ValueError(f"该申请已被处理，当前状态: {application.status}")
        return application

    async def validate_application_request(
        self,
        target_id: int,
        guild_id: int,
        applicant_id: int,
        cover_image_url: str | None,
        target_scope: Optional[str] = None,
    ) -> ApplicationResult:
        """验证Banner申请请求，自动检测目标类型（Thread / Channel / 按需索引）。"""
        cover_url = cover_image_url.strip() if cover_image_url else None
        if cover_url and not cover_url.startswith(("http://", "https://")):
            return ApplicationResult(
                success=False,
                message="封面图链接必须是有效的URL（以http://或https://开头）",
            )

        # 验证展示范围（如果提供）
        if target_scope is not None:
            scope = target_scope.strip()
            if scope != "global" and not scope.isdigit():
                return ApplicationResult(
                    success=False,
                    message="展示范围必须是'global'或有效的频道ID",
                )

        # 查 Thread 表 → 找到 → target_type=THREAD，检查作者
        thread_repo = ThreadRepository(self.session)
        thread = await thread_repo.get_thread_with_tags(target_id)
        if thread:
            if not thread.show_flag or thread.deleted_at is not None:
                return ApplicationResult(
                    success=False,
                    message="该帖子当前不可见，无法申请Banner",
                )
            canonical_applicant_id = await resolve_business_id(
                self.session, applicant_id, source="discord"
            )
            if thread.author_id != applicant_id or (
                is_fluxer_mode() and thread.fluxer_author_id != canonical_applicant_id
            ):
                return ApplicationResult(
                    success=False,
                    message="只能为自己的帖子申请Banner",
                )
            if cover_url is None and not thread.thumbnail_urls:
                return ApplicationResult(
                    success=False,
                    message="帖子没有可用首图，请提交自定义封面图",
                )
            return ApplicationResult(
                success=True,
                message="验证通过",
                thread=ThreadDTO.from_orm(thread),
                target_type=TargetType.THREAD.value,
                guild_id=thread.guild_id,
                target_name=thread.title,
            )

        # 查 Channel 表 → 找到 → target_type=CHANNEL，跳过作者检查
        channel_result = await self.session.execute(
            select(Channel).where(Channel.channel_id == target_id)
        )
        channel = channel_result.scalar_one_or_none()
        if channel:
            if cover_url is None:
                return ApplicationResult(
                    success=False,
                    message="频道Banner必须提交自定义封面图",
                )
            return ApplicationResult(
                success=True,
                message="验证通过",
                target_type=TargetType.CHANNEL.value,
                guild_id=channel.guild_id,
                target_name=channel.name,
            )

        # 都没找到 → ChannelSyncService 按需索引
        if self.channel_sync:
            channel = await self.channel_sync.fetch_and_index(self.session, target_id)
            if channel:
                if cover_url is None:
                    return ApplicationResult(
                        success=False,
                        message="频道Banner必须提交自定义封面图",
                    )
                return ApplicationResult(
                    success=True,
                    message="验证通过",
                    target_type=TargetType.CHANNEL.value,
                    guild_id=channel.guild_id,
                    target_name=channel.name,
                )

        return ApplicationResult(
            success=False,
            message="该帖子/频道未被索引，无法申请Banner。请确保ID正确。",
        )

    async def validate_and_create_application(
        self,
        target_id: int,
        guild_id: int,
        applicant_id: int,
        cover_image_url: str | None,
        target_scope: str,
        enforce_applicant_limit: bool = False,
    ) -> ApplicationResult:
        """验证并创建Banner申请。"""
        try:
            await resolve_business_id(self.session, applicant_id, source="discord")
            # 所有入口创建均加锁，只有 Bot 开启占位限制。
            await self.app_repo.lock_applicant(applicant_id)
            if enforce_applicant_limit and await self.has_ongoing_banner(applicant_id):
                await self.session.rollback()
                return ApplicationResult(
                    success=False, message=self.APPLICANT_LIMIT_MESSAGE
                )

            validation = await self.validate_application_request(
                target_id=target_id,
                guild_id=guild_id,
                applicant_id=applicant_id,
                cover_image_url=cover_image_url,
                target_scope=target_scope,
            )

            if not validation.success:
                await self.session.rollback()
                return validation

            # 确定 channel_id：Thread 用帖子所属频道，Channel 用频道自身
            if validation.target_type == TargetType.CHANNEL.value:
                channel_id = target_id
            elif validation.thread:
                channel_id = validation.thread.channel_id
            else:
                channel_id = guild_id

            cover_url = cover_image_url.strip() if cover_image_url else None
            scope = target_scope.strip()

            application = await self.app_repo.create(
                thread_id=target_id,
                channel_id=channel_id,
                applicant_id=applicant_id,
                cover_image_url=cover_url,
                target_scope=scope,
                target_type=validation.target_type,
            )
            await self.session.commit()
            await self.session.refresh(application)

            logger.info(
                f"用户 {applicant_id} 提交了Banner申请，目标ID: {target_id}，"
                f"类型: {validation.target_type}，范围: {scope}"
            )

            validation.application = application
            return validation
        except Exception:
            await self.session.rollback()
            raise

    async def approve_application(
        self, application_id: int, reviewer_id: int
    ) -> BannerApprovalResult:
        """批准申请并将banner加入轮播或等待列表。"""
        try:
            await resolve_business_id(self.session, reviewer_id, source="discord")
            application = await self._get_pending_for_review(application_id)
            await resolve_business_id(
                self.session, application.applicant_id, source="discord"
            )

            # 根据 target_type 获取标题
            if application.target_type == TargetType.CHANNEL.value:
                channel_result = await self.session.execute(
                    select(Channel).where(Channel.channel_id == application.thread_id)
                )
                channel = channel_result.scalar_one_or_none()
                target_title = channel.name if channel else "未知频道"
            else:
                thread_repo = ThreadRepository(self.session)
                thread_obj = await thread_repo.get_thread_with_tags(
                    application.thread_id
                )
                if not thread_obj:
                    raise ValueError("帖子不存在")
                target_title = thread_obj.title

            # 更新申请状态
            now = utc_now()
            application.status = ApplicationStatus.APPROVED.value
            application.reviewed_at = now
            application.reviewer_id = reviewer_id
            application.fluxer_reviewer_id = (
                await resolve_business_id(self.session, reviewer_id, source="discord")
                if is_fluxer_mode()
                else None
            )

            entered_carousel = await self.enqueue_application(application, target_title)

            # 同批拒绝与批准占位一起提交，失败时全部回滚。
            reason = f"同一申请人的 Banner 申请 #{application.id} 已通过审核，本申请自动拒绝。"
            rejected = await self.app_repo.reject_other_pending(
                application.applicant_id, application_id, reviewer_id, now, reason
            )
            result = BannerApprovalResult(
                application=BannerReviewApplication.from_application(application),
                entered_carousel=entered_carousel,
                auto_rejected=[
                    BannerReviewApplication.from_application(item) for item in rejected
                ],
            )
            await self.session.commit()
            return result
        except Exception:
            await self.session.rollback()
            raise

    async def reject_application(
        self, application_id: int, reviewer_id: int, reason: str
    ) -> BannerReviewApplication:
        """拒绝申请并返回会话独立的审核快照。"""
        try:
            await resolve_business_id(self.session, reviewer_id, source="discord")
            application = await self._get_pending_for_review(application_id)
            await resolve_business_id(
                self.session, application.applicant_id, source="discord"
            )

            now = utc_now()
            application.status = ApplicationStatus.REJECTED.value
            application.reviewed_at = now
            application.reviewer_id = reviewer_id
            application.fluxer_reviewer_id = (
                await resolve_business_id(self.session, reviewer_id, source="discord")
                if is_fluxer_mode()
                else None
            )
            application.reject_reason = reason

            result = BannerReviewApplication.from_application(application)
            await self.session.commit()
            return result
        except Exception:
            await self.session.rollback()
            raise

    async def lock_scope(self, channel_id: int | None) -> None:
        """串行化同一展示范围的审批、清理与 FIFO 晋升。"""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {
                "key": f"banner:scope:{channel_id if channel_id is not None else 'global'}"
            },
        )

    async def enqueue_application(
        self, application: BannerApplication, title: str
    ) -> bool:
        """使用既有轮播和候补队列，旧候补优先占据空位。"""
        channel_id = (
            None
            if application.target_scope == "global"
            else int(application.target_scope)
        )
        await self.lock_scope(channel_id)
        await self._refill_scope_to_capacity(channel_id)
        active = await self.carousel_repo.get_count(channel_id)
        values = {
            "thread_id": application.thread_id,
            "channel_id": channel_id,
            "cover_image_url": application.cover_image_url,
            "title": title,
            "target_type": application.target_type,
            "application_id": application.id,
            "applicant_id": application.applicant_id,
            "work_id": application.work_id,
            "fluxer_owner_id": application.fluxer_owner_id,
            "fluxer_applicant_id": application.fluxer_applicant_id,
        }
        if active < self._get_scope_capacity(channel_id):
            await self.carousel_repo.add(
                **values, duration_days=self.BANNER_DURATION_DAYS
            )
            return True
        await self.waitlist_repo.add(**values)
        return False

    def _get_scope_capacity(self, channel_id: Optional[int]) -> int:
        """获取全频道或单频道范围的轮播容量。"""
        return (
            self.GLOBAL_MAX_BANNERS if channel_id is None else self.CHANNEL_MAX_BANNERS
        )

    async def _refill_scope_to_capacity(self, channel_id: Optional[int]) -> int:
        """在轮播存在空位时按 FIFO 从等待队列补足。"""
        await self.lock_scope(channel_id)
        current_count = await self.carousel_repo.get_count(channel_id)
        available_slots = max(0, self._get_scope_capacity(channel_id) - current_count)
        promoted_count = 0

        # 补位次数受范围容量约束，达到或超过容量时不再补位。
        for _ in range(available_slots):
            waitlist_item = await self.waitlist_repo.pop(channel_id)
            if waitlist_item is None:
                break
            if waitlist_item.applicant_id is not None:
                await resolve_business_id(
                    self.session, waitlist_item.applicant_id, source="discord"
                )
            await self.carousel_repo.add(
                thread_id=waitlist_item.thread_id,
                channel_id=waitlist_item.channel_id,
                cover_image_url=waitlist_item.cover_image_url,
                title=waitlist_item.title,
                duration_days=self.BANNER_DURATION_DAYS,
                target_type=waitlist_item.target_type,
                application_id=waitlist_item.application_id,
                applicant_id=waitlist_item.applicant_id,
                work_id=waitlist_item.work_id,
                fluxer_owner_id=waitlist_item.fluxer_owner_id,
                fluxer_applicant_id=waitlist_item.fluxer_applicant_id,
            )
            promoted_count += 1

        return promoted_count

    async def cleanup_expired_banners(self) -> int:
        """清理过期的banner并从等待列表补充。"""
        expired = await self.carousel_repo.get_expired()
        locked_scopes = {item.channel_id for item in expired}
        for scope in sorted(
            locked_scopes, key=lambda value: (value is not None, value or 0)
        ):
            await self.lock_scope(scope)
        # 等待范围锁后重新读取，忽略已由其他清理器处理的记录。
        expired = [
            item
            for item in await self.carousel_repo.get_expired()
            if item.channel_id in locked_scopes
        ]

        affected_channel_ids: set[int | None] = set()
        for banner in expired:
            await self.carousel_repo.delete(banner)
            affected_channel_ids.add(banner.channel_id)

        # 每个受影响范围只计算一次空位，再按新容量补足。
        for channel_id in affected_channel_ids:
            await self._refill_scope_to_capacity(channel_id)

        await self.session.commit()
        return len(expired)

    async def delete_banner_by_thread(self, thread_id: int) -> DeleteBannerResult:
        """根据 thread_id 从轮播或等待列表中删除 Banner。"""
        carousel_items = await self.carousel_repo.get_by_thread(thread_id)
        waitlist_items = await self.waitlist_repo.get_by_thread(thread_id)

        scopes = {item.channel_id for item in [*carousel_items, *waitlist_items]}
        for scope in sorted(scopes, key=lambda value: (value is not None, value or 0)):
            await self.lock_scope(scope)
        if scopes:
            carousel_items = await self.carousel_repo.get_by_thread(thread_id)
            waitlist_items = await self.waitlist_repo.get_by_thread(thread_id)

        if not carousel_items and not waitlist_items:
            return DeleteBannerResult(
                success=False,
                message="未找到该帖子ID对应的Banner记录，请在轮播列表或等待列表中确认。",
            )

        if len(carousel_items) + len(waitlist_items) > 1:
            parts = []
            for item in carousel_items:
                scope = (
                    "全频道" if item.channel_id is None else f"频道 {item.channel_id}"
                )
                parts.append(f"  • 轮播列表: {item.title[:40]} (范围: {scope})")
            for item in waitlist_items:
                scope = (
                    "全频道" if item.channel_id is None else f"频道 {item.channel_id}"
                )
                parts.append(f"  • 等待列表: {item.title[:40]} (范围: {scope})")
            return DeleteBannerResult(
                success=False,
                message="该帖子ID对应多条Banner记录，删除时存在歧义：\n"
                + "\n".join(parts),
            )

        if carousel_items:
            banner = carousel_items[0]
            channel_id = banner.channel_id
            title = banner.title
            scope_label = "全频道" if channel_id is None else f"频道 {channel_id}"

            await self.carousel_repo.delete(banner)
            promoted_count = await self._refill_scope_to_capacity(channel_id)

            await self.session.commit()

            return DeleteBannerResult(
                success=True,
                message=f"已从轮播列表中删除Banner「{title}」",
                deleted_from="carousel",
                thread_id=thread_id,
                banner_title=title,
                scope_label=scope_label,
                promoted_from_waitlist=promoted_count > 0,
            )

        if waitlist_items:
            banner = waitlist_items[0]
            channel_id = banner.channel_id
            title = banner.title
            scope_label = "全频道" if channel_id is None else f"频道 {channel_id}"

            await self.waitlist_repo.delete(banner)
            await self.session.commit()

            return DeleteBannerResult(
                success=True,
                message=f"已从等待列表中删除Banner「{title}」",
                deleted_from="waitlist",
                thread_id=thread_id,
                banner_title=title,
                scope_label=scope_label,
                promoted_from_waitlist=False,
            )

        return DeleteBannerResult(
            success=False,
            message="删除Banner时发生未预期的错误",
        )

    async def get_active_banners(
        self,
        channel_id: Optional[int] = None,
        channel_ids: Optional[List[int]] = None,
        all_channels: bool = False,
    ) -> List[BannerCarousel]:
        """兼容单频道调用并获取有序的多频道活跃 Banner。"""
        effective_channel_ids = list(channel_ids or [])
        if channel_id is not None:
            effective_channel_ids.append(channel_id)
        effective_channel_ids = list(dict.fromkeys(effective_channel_ids))
        return await self.carousel_repo.get_active(
            channel_ids=effective_channel_ids, all_channels=all_channels
        )

    async def update_review_message_info(
        self, application_id: int, review_message_id: int, review_thread_id: int
    ) -> BannerReviewApplication | None:
        """加锁回填消息信息并返回最新状态，供延迟发送后同步审核结果。"""
        try:
            application = await self.app_repo.get_by_id(application_id)
            if application is None:
                return None
            await self.app_repo.lock_applicant(application.applicant_id)
            await self.app_repo.update_review_message_info(
                application_id, review_message_id, review_thread_id
            )
            snapshot = await self.get_review_snapshot(application_id)
            await self.session.commit()
            return snapshot
        except Exception:
            await self.session.rollback()
            raise

    async def get_application_by_review_message(
        self, review_message_id: int
    ) -> Optional[BannerApplication]:
        """通过审核消息ID获取申请记录。"""
        return await self.app_repo.get_by_review_message(review_message_id)


async def send_review_message(
    bot: "MyBot",
    session_factory,
    application: BannerApplication,
    config: dict,
    guild_id: Optional[int] = None,
) -> bool:
    """通过统一投递服务发送审核消息并同步最新状态。"""
    from banner.banner_review_message_service import BannerReviewMessageService

    if application.id is None:
        return False
    publisher = BannerReviewMessageService(bot, session_factory, config)
    return await publisher.send(application.id, guild_id) is not None
