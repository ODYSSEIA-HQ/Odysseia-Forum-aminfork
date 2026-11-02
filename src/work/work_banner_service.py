"""首页推荐领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from sqlalchemy import select, func, or_, text
from fastapi import HTTPException
from models import BannerApplication, BannerCarousel, BannerWaitlist, User
from banner.banner_service import BannerService
from shared.time_utils import utc_now
from models.thread import Thread
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkBannerService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def banner(self, command: WorkCommandQo):
        """作品 Banner 受内容偏好约束，频道赛事 Banner 保持现有展示权限。"""
        principal = command.actor
        from models.banner_carousel import BannerCarousel

        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(BannerCarousel)
                        .where(
                            BannerCarousel.start_time <= utc_now(),
                            BannerCarousel.end_time > utc_now(),
                        )
                        .order_by(BannerCarousel.position)
                    )
                )
                .scalars()
                .all()
            )
            thread_ids = [row.thread_id for row in rows if row.target_type == 1]
            mappings = dict(
                (
                    await session.execute(
                        select(Thread.thread_id, Thread.work_id).where(
                            Thread.platform == "discord",
                            Thread.thread_id.in_(thread_ids),
                        )
                    )
                ).all()
            )
            work_ids = list(
                {value for value in mappings.values() if value is not None}
                | {row.work_id for row in rows if row.work_id is not None}
            )
            works, _ = await WorkRepository(session).page(
                principal, 0, 100, work_ids=work_ids
            )
            projected = {
                work["id"]: work for work in await WorkService(session).project(works)
            }
            items = []
            for row in rows:
                if row.target_type == 2:
                    items.append(
                        {
                            "kind": "channel",
                            "id": str(row.id),
                            "title": row.title,
                            "platform": "discord",
                            "guild_id": str(
                                principal["forums"].get("discord_guild_id", "")
                            ),
                            "channel_id": str(row.channel_id or row.thread_id),
                            "image_url": row.cover_image_url,
                        }
                    )
                else:
                    work = projected.get(
                        str(row.work_id or mappings.get(row.thread_id))
                    )
                    if work and work.get("sync_status") != "pending":
                        items.append(
                            {
                                "kind": "work",
                                "id": str(row.id),
                                "work": work,
                                "image_url": row.cover_image_url
                                or (work["images"][0] if work["images"] else None),
                            }
                        )
            return {
                "items": items,
                "total": len(items),
                "offset": 0,
                "limit": len(items),
            }

    async def apply(self, work_id: int, command: WorkCommandQo):
        """作者提交真实申请，业务归属不覆盖历史申请人。"""
        actor = command.actor
        scope = str(command.values.get("target_scope", "global"))
        allowed = {item["id"] for item in self.available_scopes(actor)}
        if scope != "global" and scope not in allowed:
            raise HTTPException(422, "请选择允许的展示范围")
        cover = command.values.get("cover_image_url")
        if cover is not None and (
            not isinstance(cover, str)
            or len(cover) > 2048
            or not cover.startswith(("https://", "http://"))
        ):
            raise HTTPException(422, "封面必须是有效图片链接")
        async with self.sessions() as session:
            work = await WorkService(session).visible(work_id, actor, owner=True)
            if work.source_platform == "fluxer" and not work.source_thread_id:
                raise HTTPException(409, "作品主帖仍在同步，请稍后申请")
            if work.author_id != actor.id:
                raise HTTPException(403, "仅作品作者可提交申请")
            if not cover and not work.images:
                raise HTTPException(422, "作品没有首图，请上传图片或提供封面")
            await self.lock_owner(session, actor.id)
            if await self.has_placement(session, actor.id):
                raise HTTPException(409, BannerService.APPLICANT_LIMIT_MESSAGE)
            existing = (
                (
                    await session.execute(
                        select(BannerApplication).where(
                            BannerApplication.fluxer_owner_id == actor.id,
                            BannerApplication.work_id == work_id,
                            BannerApplication.target_scope == scope,
                            BannerApplication.status == "pending",
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                return self.application_dto(existing)
            row = BannerApplication(
                thread_id=None,
                channel_id=None,
                work_id=work_id,
                target_type=3,
                applicant_id=int(actor.discord_user_id)
                if actor.discord_user_id
                else None,
                fluxer_applicant_id=actor.id,
                fluxer_owner_id=actor.id,
                target_scope=scope,
                cover_image_url=cover,
                status="pending",
            )
            session.add(row)
            await session.commit()
            return self.application_dto(row)

    async def applications(
        self, command: WorkCommandQo, offset: int, limit: int, *, admin=False
    ):
        """按真实申请事实分页，管理审核与申请历史共用同一集合。"""
        if admin and not command.actor.is_admin:
            raise HTTPException(403, "需要管理权限")
        async with self.sessions() as session:
            filters = (
                [] if admin else [BannerApplication.fluxer_owner_id == command.actor.id]
            )
            statement = select(BannerApplication).where(*filters)
            total = (
                await session.execute(
                    select(func.count()).select_from(BannerApplication).where(*filters)
                )
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        statement.order_by(
                            BannerApplication.applied_at.desc(),
                            BannerApplication.id.desc(),
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            ids = [row.id for row in rows]
            placements = {}
            if ids:
                for model, label in (
                    (BannerWaitlist, "waitlist"),
                    (BannerCarousel, "carousel"),
                ):
                    query = select(model.application_id).where(
                        model.application_id.in_(ids)
                    )
                    if model is BannerCarousel:
                        query = query.where(BannerCarousel.end_time > utc_now())
                    placements.update(
                        {
                            value: label
                            for value in (await session.execute(query)).scalars().all()
                        }
                    )
            return {
                "items": [
                    self.application_dto(row, placements.get(row.id)) for row in rows
                ],
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def review(self, application_id: int, command: WorkCommandQo):
        """管理审批直接进入既有轮播或 FIFO 候补，审计保留实际主体。"""
        actor = command.actor
        if not actor.is_admin:
            raise HTTPException(403, "需要管理权限")
        approve = command.values.get("approve")
        reason = command.values.get("reason") or None
        if not isinstance(approve, bool) or (
            reason is not None and (not isinstance(reason, str) or len(reason) > 2000)
        ):
            raise HTTPException(422, "无效审核请求")
        async with self.sessions() as session:
            row = await session.get(BannerApplication, application_id)
            if row is None:
                raise HTTPException(404, "申请不存在")
            owner = row.fluxer_owner_id or row.fluxer_applicant_id
            if not owner:
                raise HTTPException(409, "历史申请需要先完成身份映射")
            await self.lock_owner(session, owner)
            row = await session.get(
                BannerApplication,
                application_id,
                populate_existing=True,
                with_for_update=True,
            )
            if row.status != "pending":
                raise HTTPException(409, "该申请已处理")
            placement = None
            if approve:
                if row.target_scope not in {
                    item["id"] for item in self.available_scopes(actor)
                }:
                    raise HTTPException(409, "申请展示范围已停用")
                if row.target_type != 3 or row.work_id is None:
                    raise HTTPException(409, "旧 Discord 申请请沿既有审核入口处理")
                work = await WorkService(session).visible(row.work_id, actor)
                if work.source_platform == "fluxer" and not work.source_thread_id:
                    raise HTTPException(409, "作品主帖仍在同步")
                if work.author_id != owner:
                    raise HTTPException(409, "作品归属已变更，请重新申请")
                if await self.has_placement(session, owner):
                    raise HTTPException(409, BannerService.APPLICANT_LIMIT_MESSAGE)
                if not row.cover_image_url and not work.images:
                    raise HTTPException(409, "作品没有可用首图")
                entered = await BannerService(session).enqueue_application(
                    row, work.title
                )
                placement = "carousel" if entered else "waitlist"
            now = utc_now()
            row.status = "approved" if approve else "rejected"
            row.reviewed_at = now
            row.reviewer_id = (
                int(actor.discord_user_id) if actor.discord_user_id else None
            )
            row.fluxer_reviewer_id = actor.id
            row.reject_reason = reason if not approve else None
            if approve:
                from sqlalchemy import update

                await session.execute(
                    update(BannerApplication)
                    .where(
                        BannerApplication.fluxer_owner_id == owner,
                        BannerApplication.id != row.id,
                        BannerApplication.status == "pending",
                    )
                    .values(
                        status="rejected",
                        reviewed_at=now,
                        reviewer_id=row.reviewer_id,
                        fluxer_reviewer_id=actor.id,
                        reject_reason=f"同一申请人的 Banner 申请 #{row.id} 已通过审核，本申请自动拒绝。",
                    )
                )
            await session.commit()
            return self.application_dto(row, placement)

    async def lock_owner(self, session, owner: str):
        """跨协议共享同一业务申请人锁，身份合并后仍保持单一占位。"""
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"identity-merge:{owner}"},
        )
        user = await session.get(User, owner, populate_existing=True)
        if user is None or user.merged_into_id:
            raise HTTPException(409, "业务归属已变更，请刷新后重试")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"banner:owner:{owner}"},
        )

    async def has_placement(self, session, owner: str):
        """同一快照检查活跃轮播及候补，避免晋升产生占位空隙。"""
        active = (
            select(BannerCarousel.id)
            .where(
                func.coalesce(
                    BannerCarousel.fluxer_owner_id, BannerCarousel.fluxer_applicant_id
                )
                == owner,
                BannerCarousel.end_time > utc_now(),
                BannerCarousel.application_id.is_not(None),
            )
            .exists()
        )
        waiting = (
            select(BannerWaitlist.id)
            .where(
                func.coalesce(
                    BannerWaitlist.fluxer_owner_id, BannerWaitlist.fluxer_applicant_id
                )
                == owner,
                BannerWaitlist.application_id.is_not(None),
            )
            .exists()
        )
        return bool((await session.execute(select(or_(active, waiting)))).scalar_one())

    def application_dto(self, row, placement=None):
        """只输出可表达的外部标识，不把 Work ID 放入 Discord 字段。"""
        return {
            "id": str(row.id),
            "work_id": str(row.work_id) if row.work_id is not None else None,
            "thread_id": str(row.thread_id) if row.thread_id is not None else None,
            "target_type": row.target_type,
            "target_scope": row.target_scope,
            "cover_image_url": row.cover_image_url,
            "status": row.status,
            "applied_at": row.applied_at,
            "reviewed_at": row.reviewed_at,
            "reject_reason": row.reject_reason,
            "placement": placement,
            "applicant_id": row.fluxer_applicant_id,
            "reviewer_id": row.fluxer_reviewer_id,
        }

    def available_scopes(self, actor):
        """只公开配置允许的展示范围与可读名称。"""
        labels = {
            "normal": "普通论坛",
            "normal_nsfw": "普通 NSFW 论坛",
            "abyss": "深渊论坛",
            "abyss_nsfw": "深渊 NSFW 论坛",
        }
        scopes = {"global": "全站"}
        for key, label in labels.items():
            value = actor.forums.get(key)
            if (
                value
                and (not key.endswith("nsfw") or actor.show_nsfw)
                and (not key.startswith("abyss") or actor.can_view_abyss)
            ):
                scopes[str(value)] = label
        configured = actor.config.get("banner", {}).get("available_channels", [])
        if isinstance(configured, dict):
            for value, label in configured.items():
                if str(value).isdigit():
                    scopes[str(value)] = str(label)
        else:
            for entry in configured:
                value = entry.get("id") if isinstance(entry, dict) else entry
                label = entry.get("name") if isinstance(entry, dict) else None
                if value and str(value).isdigit():
                    scopes[str(value)] = str(label or "索引频道")
        return [{"id": value, "name": label} for value, label in scopes.items()]

    async def scopes(self, work_id: int, command: WorkCommandQo):
        """作者选择受限范围，不暴露其他服务配置。"""
        async with self.sessions() as session:
            work = await WorkService(session).visible(
                work_id, command.actor, owner=True
            )
            if work.author_id != command.actor.id:
                raise HTTPException(403, "仅作品作者可申请")
            return {"items": self.available_scopes(command.actor)}
