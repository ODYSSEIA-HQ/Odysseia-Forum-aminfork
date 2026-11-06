"""随机作品发现：沿用 V1 显式配方，不隐式套用普通搜索偏好。"""

from fastapi import HTTPException
from sqlalchemy import or_
from api.v2.schemas.work_search_request import WorkSearchRequest
from models.work import Work
from shared.tag_error import TagError
from work.work_repository import WorkRepository
from work.work_service import WorkService


class WorkRandomService:
    def __init__(self, sessions):
        """保存请求内使用的会话工厂与服务配置。"""
        self.sessions = sessions

    async def draw(
        self,
        principal,
        limit,
        channels,
        excluded_channels,
        included_tags,
        excluded_tags,
        tag_logic,
    ):
        """按显式频道与标签配方随机抽取可见作品。"""
        # 构建独立检索参数，避免混入用户上次普通搜索的正选条件。
        query = WorkSearchRequest(
            channel_ids=channels,
            exclude_channel_ids=excluded_channels or [],
            include_tags=included_tags or [],
            exclude_tags=excluded_tags or [],
            tag_logic=tag_logic,
            apply_preferences=False,
        )
        # 配置中的旧频道 ID 属于 Discord，不误伤同数字的 Fluxer 来源。
        ignored = (
            principal.get("config", {})
            .get("discovery", {})
            .get("ignore_channel_ids", [])
        )
        filters = (
            [
                or_(
                    Work.source_platform != "discord",
                    Work.source_channel_id.not_in([str(value) for value in ignored]),
                )
            ]
            if ignored
            else []
        )
        async with self.sessions() as session:
            try:
                rows, _ = await WorkRepository(session).page(
                    principal,
                    0,
                    limit,
                    advanced=query,
                    extra_filters=filters,
                    random_order=True,
                )
            except TagError as exc:
                raise HTTPException(exc.status, exc.detail) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return await WorkService(session).project(rows)
