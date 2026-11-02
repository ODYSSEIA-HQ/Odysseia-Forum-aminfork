from identity.runtime import is_fluxer_mode, uses_canonical_storage
from identity.business_identity import resolve_business_id
from datetime import datetime
from typing import List, Optional

from sqlalchemy import text, update, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import desc, select

from models import BannerApplication, User
from shared.enum import ApplicationStatus


class BannerApplicationRepository:
    """banner_application 表的数据库操作。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        thread_id: int,
        channel_id: int,
        applicant_id: int,
        cover_image_url: str | None,
        target_scope: str,
        target_type: int = 1,
        work_id: int | None = None,
        fluxer_owner_id: str | None = None,
    ) -> BannerApplication:
        """插入一条 PENDING 状态的申请记录。"""
        application = BannerApplication(
            thread_id=thread_id,
            work_id=work_id,
            fluxer_owner_id=fluxer_owner_id
            or (
                str(
                    await resolve_business_id(
                        self.session, applicant_id, source="discord"
                    )
                )
                if uses_canonical_storage() and applicant_id is not None
                else None
            ),
            channel_id=channel_id,
            applicant_id=applicant_id,
            fluxer_applicant_id=(
                await resolve_business_id(self.session, applicant_id, source="discord")
                if is_fluxer_mode() and applicant_id
                else None
            ),
            cover_image_url=cover_image_url,
            target_scope=target_scope,
            target_type=target_type,
            status=ApplicationStatus.PENDING.value,
            applied_at=datetime.now().replace(microsecond=0),
        )
        self.session.add(application)
        await self.session.flush()
        return application

    async def lock_applicant(self, applicant_id: int) -> None:
        """在事务内串行化同一申请人的申请创建和审核。"""
        owner = (
            str(await resolve_business_id(self.session, applicant_id, source="discord"))
            if uses_canonical_storage()
            else str(applicant_id)
        )
        if uses_canonical_storage():
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"identity-merge:{owner}"},
            )
            user = await self.session.get(User, owner, populate_existing=True)
            if user is None or user.merged_into_id:
                raise ValueError("业务归属已变更，请刷新后重试")
        # 命名锁键隔离其他业务，事务结束时 PostgreSQL 自动释放。
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"banner:owner:{owner}"},
        )

    async def reject_other_pending(
        self,
        applicant_id: int,
        approved_id: int,
        reviewer_id: int,
        reviewed_at: datetime,
        reason: str,
    ) -> List[BannerApplication]:
        """一次更新并返回同申请人的其他待审核申请。"""
        owner = (
            str(await resolve_business_id(self.session, applicant_id, source="discord"))
            if uses_canonical_storage()
            else None
        )
        # 状态条件保护已审核历史，RETURNING 避免逐条更新和查询。
        result = await self.session.execute(
            update(BannerApplication)
            .where(
                or_(
                    BannerApplication.applicant_id == applicant_id,
                    BannerApplication.fluxer_owner_id == owner
                    if owner is not None
                    else False,
                ),
                BannerApplication.id != approved_id,
                BannerApplication.status == ApplicationStatus.PENDING.value,
            )
            .values(
                status=ApplicationStatus.REJECTED.value,
                reviewer_id=reviewer_id,
                fluxer_reviewer_id=(
                    await resolve_business_id(
                        self.session, reviewer_id, source="discord"
                    )
                    if is_fluxer_mode() and reviewer_id
                    else None
                ),
                reviewed_at=reviewed_at,
                reject_reason=reason,
            )
            .returning(BannerApplication)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def get_by_id(self, application_id: int) -> Optional[BannerApplication]:
        """按主键查询。"""
        result = await self.session.execute(
            select(BannerApplication)
            .where(BannerApplication.id == application_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_by_review_message(
        self, review_message_id: int
    ) -> Optional[BannerApplication]:
        """通过审核消息 ID 反查。"""
        result = await self.session.execute(
            select(BannerApplication).where(
                BannerApplication.review_message_id == review_message_id
            )
        )
        return result.scalar_one_or_none()

    async def get_history_by_thread_id(self, thread_id: int) -> List[BannerApplication]:
        """查询同一 thread_id 的非 pending 申请，按 applied_at 倒序。

        仅返回有 review_message_id 的记录，确保能生成审核消息跳转链接。
        """
        result = await self.session.execute(
            select(BannerApplication)
            .where(
                BannerApplication.thread_id == thread_id,
                BannerApplication.status != ApplicationStatus.PENDING.value,
                BannerApplication.review_message_id.is_not(None),
            )
            .order_by(desc(BannerApplication.applied_at))
        )
        return list(result.scalars().all())

    async def update_review_message_info(
        self,
        application_id: int,
        review_message_id: int,
        review_thread_id: int,
    ) -> bool:
        """回填审核消息 ID 和所在线程 ID。"""
        result = await self.session.execute(
            select(BannerApplication)
            .where(BannerApplication.id == application_id)
            .execution_options(populate_existing=True)
        )
        application = result.scalar_one_or_none()
        if application:
            application.review_message_id = review_message_id
            application.review_thread_id = review_thread_id
            return True
        return False
