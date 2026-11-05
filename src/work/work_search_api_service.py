"""作品搜索领域服务。"""

from dto.work.work_command_qo import WorkCommandQo
from fastapi import HTTPException
from work.work_repository import WorkRepository
from work.work_service import WorkService
from api.v2.schemas.work_search_request import WorkSearchRequest


class WorkSearchApiService:
    """协调领域权限、仓储、可靠任务和请求事务。"""

    def __init__(self, sessions):
        """复用由路由注入的会话工厂，跨表操作在服务层协调。"""
        self.sessions = sessions

    async def list_works(
        self,
        offset: int,
        limit: int,
        q: str | None,
        is_nsfw: bool | None,
        is_abyss: bool | None,
        author_id: str | None,
        tag_id: int | None,
        command: WorkCommandQo,
    ):
        """所有列表统一过滤隐藏分区后分页。"""
        principal = command.actor
        async with self.sessions() as session:
            rows, total = await WorkRepository(session).page(
                principal, offset, limit, q, is_nsfw, is_abyss, author_id, tag_id
            )
            return {
                "items": await WorkService(session).project(rows),
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    async def advanced_search(self, query: WorkSearchRequest, command: WorkCommandQo):
        """高级搜索使用 V1 相同请求语法和算法，返回作品分页。"""
        principal = command.actor
        body = query.model_copy(deep=True)
        from shared.tag_error import TagError
        from core.preferences_repository import PreferencesRepository
        from search.search_preferences import merge_search_preferences
        from identity.canonical_user_id import CanonicalUserId
        from work.work_preferences_service import search_preference_guild

        inherited_discord_channels = False
        async with self.sessions() as session:
            if body.apply_preferences:
                try:
                    preferences = await PreferencesRepository(
                        session
                    ).get_user_preferences(
                        CanonicalUserId(str(principal["id"])),
                        search_preference_guild(principal),
                    )
                except ValueError as exc:
                    raise HTTPException(503, "旧作者偏好身份映射尚未就绪") from exc
                explicit_fields = body.model_fields_set.copy()
                if preferences:
                    inherited_discord_channels = (
                        bool(preferences.preferred_channels)
                        and "channel_ids" not in explicit_fields
                    )
                    merge_search_preferences(body, preferences)
                if "limit" not in explicit_fields:
                    # 旧数据可能绕过 API 校验，读取时限制查询规模但不改写偏好。
                    body.limit = max(
                        1, min(100, preferences.ui_page_size if preferences else 48)
                    )
            try:
                rows, total = await WorkRepository(session).page(
                    principal,
                    body.offset,
                    body.limit,
                    is_nsfw=body.is_nsfw,
                    is_abyss=body.is_abyss,
                    advanced=body,
                    channel_platform="discord" if inherited_discord_channels else None,
                )
            except TagError as exc:
                raise HTTPException(exc.status, exc.detail) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            result = await WorkService(session).project(rows)
            ordering = (
                body.custom_base_sort
                if body.sort_method == "custom"
                else body.sort_method
            )
            if ordering not in ("created_at", "collected_at"):
                from work.work_statistics_repository import WorkStatisticsRepository

                await WorkStatisticsRepository(session).record_impressions(
                    [row.id for row in rows]
                )
                await session.commit()
            return {
                "items": result,
                "total": total,
                "offset": body.offset,
                "limit": body.limit,
            }
