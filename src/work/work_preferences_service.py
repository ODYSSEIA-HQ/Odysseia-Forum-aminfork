"""V2 完整搜索偏好，普通偏好沿用旧表，Forum NSFW 使用本地开关并在切换时双写。"""

from fastapi import HTTPException
from pydantic import ValidationError
from api.v2.schemas.work_preferences_response import WorkPreferencesResponse
from api.v2.schemas.work_preferences_update_request import WorkPreferencesUpdateRequest
from core.preferences_repository import PreferencesRepository
from dto.preferences import UserSearchPreferencesDTO
from identity.canonical_user_id import CanonicalUserId
from shared.redis_client import RedisManager
from shared.time_parser import parse_time_string


def search_preference_guild(principal):
    """旧搜索表仅按配置的 Discord 主服务器分区，不接受请求自选作用域。"""
    try:
        guild = int(principal["forums"]["discord_guild_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(503, "搜索偏好服务器尚未配置") from exc
    if guild <= 0:
        raise HTTPException(503, "搜索偏好服务器尚未配置")
    return guild


def nsfw_preference_guild(principal):
    """Fluxer 成人内容权威使用原生 guild，绝不使用 Discord guild。"""
    guild = str(principal["forums"].get("guild_id") or "")
    if not guild:
        raise HTTPException(503, "成人内容偏好服务器尚未配置")
    return guild


class WorkPreferencesService:
    """协调搜索偏好、本地显示开关与远端双写。"""

    def __init__(self, sessions):
        """保存会话工厂，每次操作在独立事务中读取或保存偏好。"""
        self.sessions = sessions

    @staticmethod
    def _response(principal, prefs, guild, nsfw):
        """从独立偏好 DTO 和权威状态生成对外响应。"""
        defaults = UserSearchPreferencesDTO(user_id=str(principal["id"]))
        return WorkPreferencesResponse(
            **(prefs or defaults).model_dump(exclude={"user_id"}),
            user_id=str(principal["id"]),
            guild_id=str(guild),
            is_first_time=prefs is None,
            show_nsfw=nsfw["show_nsfw"],
            nsfw_preference_version=nsfw["version"],
            nsfw_guild_id=nsfw["guild_id"],
            nsfw_eligible=nsfw["nsfw_eligible"],
        ).model_dump(mode="json")

    async def _nsfw(self, principal):
        """复用本次认证的本地开关及远端年龄/CAS 状态。"""
        # 认证快照带有版本时直接使用，不创建跨请求的展示缓存。
        version = principal.get("nsfw_preference_version")
        if isinstance(version, int) and not isinstance(version, bool) and version >= 0:
            return {
                "guild_id": nsfw_preference_guild(principal),
                "show_nsfw": (
                    principal.get("nsfw_preference_show_nsfw")
                    if principal.get("nsfw_preference_show_nsfw") is not None
                    else principal["show_nsfw"]
                ),
                "nsfw_eligible": principal.get("nsfw_eligible", False),
                "version": version,
            }
        from identity.fluxer_preferences_client import FluxerPreferencesClient

        snapshot = await FluxerPreferencesClient().get_preferences(
            principal["id"], nsfw_preference_guild(principal)
        )
        # 没有认证快照的内部调用也读取本地行，不使用远端显示值。
        from models.user import User

        async with self.sessions() as session:
            profile = await session.get(User, str(principal["id"]))
            return {**snapshot, "show_nsfw": bool(profile and profile.show_nsfw)}

    async def preferences(self, principal):
        """读取当前成员的完整搜索偏好，不创建缺失记录。"""
        # 普通偏好只在本地会话内读取，响应使用脱离会话的 DTO。
        guild = search_preference_guild(principal)
        nsfw = await self._nsfw(principal)
        async with self.sessions() as session:
            try:
                prefs = await PreferencesRepository(session).get_user_preferences(
                    CanonicalUserId(str(principal["id"])), guild
                )
            except ValueError as exc:
                raise HTTPException(503, "旧作者偏好身份映射尚未就绪") from exc
            return self._response(principal, prefs, guild, nsfw)

    @staticmethod
    def _validate_ranges(existing, update):
        """合并现值与部分更新后检查创建和活跃时间范围。"""
        values = (existing.model_dump() if existing else {}) | update
        for start, end in (
            ("created_after", "created_before"),
            ("active_after", "active_before"),
        ):
            try:
                before = parse_time_string(values.get(end))
                after = parse_time_string(values.get(start))
                if before and after and after > before:
                    raise HTTPException(422, f"{start} 不能晚于 {end}")
            except (ValueError, OverflowError) as exc:
                raise HTTPException(422, "时间范围无效，请修改时间筛选") from exc

    async def save_preferences(self, principal, values):
        """部分更新普通偏好，独立双写成人内容开关。"""
        # 只解析显式提交的字段，避免省略值覆盖已保存偏好。
        try:
            update = WorkPreferencesUpdateRequest.model_validate(
                dict(values)
            ).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise HTTPException(422, "偏好字段或取值无效") from exc
        if not update:
            raise HTTPException(400, "请求体不能为空")
        if "show_nsfw" in update:
            if set(update) - {"show_nsfw", "expected_version"}:
                raise HTTPException(422, "成人内容开关需与其他搜索偏好分别保存")
            # 先确认普通偏好可读，避免远端成功后又因本地旧映射失败而伪报失败。
            document = await self.preferences(principal)
            from work.work_nsfw_sync_service import WorkNsfwSyncService

            nsfw = await WorkNsfwSyncService(self.sessions).save(
                principal["id"],
                nsfw_preference_guild(principal),
                update["show_nsfw"],
                update.get("expected_version", document["nsfw_preference_version"]),
            )
            return {
                **document,
                "show_nsfw": nsfw["show_nsfw"],
                "nsfw_preference_version": nsfw["version"],
                "nsfw_guild_id": nsfw["guild_id"],
                "nsfw_eligible": nsfw["nsfw_eligible"],
            }
        if "expected_version" in update:
            raise HTTPException(422, "expected_version 仅用于成人内容偏好")
        if update.get("exclude_keyword_exemption_markers", False) is None:
            update["exclude_keyword_exemption_markers"] = []
        for field in ("include_authors", "exclude_authors"):
            if update.get(field) is not None:
                update[field] = [CanonicalUserId(value) for value in update[field]]
        guild = search_preference_guild(principal)
        nsfw = await self._nsfw(principal)
        # 仓储负责单表读写，服务层协调版本状态与时间范围约束。
        async with self.sessions() as session:
            repo = PreferencesRepository(session, redis_client=RedisManager._client)
            user_id = CanonicalUserId(str(principal["id"]))
            await repo.lock_user_preferences(user_id, guild)
            try:
                existing = await repo.get_user_preferences(user_id, guild)
                self._validate_ranges(existing, update)
                prefs = await repo.save_user_preferences(user_id, update, guild)
            except ValueError as exc:
                raise HTTPException(503, "旧作者偏好身份映射尚未就绪") from exc
            return self._response(principal, prefs, guild, nsfw)
