from identity.business_identity import discord_id_for_business_id
import json
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from dto.preferences import UserSearchPreferencesDTO
from identity.runtime import uses_canonical_storage
from identity.business_identity import resolve_business_id, resolve_business_ids
from models import UserSearchPreferences
from shared.enum import CacheKeys, ConstantEnum

logger = logging.getLogger(__name__)


class PreferencesRepository:
    """封装与用户偏好设置相关的数据库操作。"""

    def __init__(self, session: AsyncSession, redis_client=None):
        self.session = session
        self._redis = redis_client

    async def get_user_preferences(
        self, user_id: int, guild_id: int = 0
    ) -> Optional[UserSearchPreferencesDTO]:
        """获取用户在指定服务器的搜索偏好设置"""
        user_id = await resolve_business_id(self.session, user_id)
        stmt = select(UserSearchPreferences).where(
            (
                UserSearchPreferences.fluxer_user_id
                if uses_canonical_storage()
                else UserSearchPreferences.user_id
            )
            == user_id,
            UserSearchPreferences.guild_id == guild_id,
        )
        result = await self.session.execute(stmt)
        prefs_orm = result.scalars().first()
        if not prefs_orm:
            return None
        if uses_canonical_storage():
            for field in ("include_authors", "exclude_authors"):
                if (
                    getattr(prefs_orm, "fluxer_" + field) is None
                    and getattr(prefs_orm, field) is not None
                ):
                    mapped = await resolve_business_ids(
                        self.session, getattr(prefs_orm, field), source="discord"
                    )
                    setattr(prefs_orm, "fluxer_" + field, mapped)
        return self._to_dto(prefs_orm)

    async def save_user_preferences(
        self, user_id: int, prefs_data: dict, guild_id: int = 0
    ) -> UserSearchPreferencesDTO:
        """创建或更新用户在指定服务器的搜索偏好设置"""
        user_id = await resolve_business_id(self.session, user_id)
        legacy_discord_id = await discord_id_for_business_id(self.session, user_id)
        stmt = select(UserSearchPreferences).where(
            (
                UserSearchPreferences.fluxer_user_id
                if uses_canonical_storage()
                else UserSearchPreferences.user_id
            )
            == user_id,
            UserSearchPreferences.guild_id == guild_id,
        )
        result = await self.session.execute(stmt)
        prefs = result.scalars().first()
        if uses_canonical_storage():
            prefs_data = dict(prefs_data)
            for field in ("include_authors", "exclude_authors"):
                if field in prefs_data:
                    value = prefs_data.pop(field)
                    prefs_data["fluxer_" + field] = (
                        await resolve_business_ids(self.session, value)
                        if value is not None
                        else None
                    )
        if prefs:
            for key, value in prefs_data.items():
                setattr(prefs, key, value)
        else:
            prefs = UserSearchPreferences(
                user_id=legacy_discord_id,
                fluxer_user_id=str(user_id) if uses_canonical_storage() else None,
                guild_id=guild_id,
                **prefs_data,
            )
        self.session.add(prefs)
        await self.session.commit()
        await self.session.refresh(prefs)

        # 写入成功后刷新 Redis 缓存
        if self._redis:
            try:
                cache_key = (
                    "fluxer:" if uses_canonical_storage() else ""
                ) + CacheKeys.USER_PREFERENCES.format(
                    user_id=user_id, guild_id=guild_id
                )
                result_dto = self._to_dto(prefs)
                await self._redis.setex(
                    cache_key,
                    int(ConstantEnum.PREF_CACHE_TTL),
                    json.dumps(result_dto.model_dump(), default=str),
                )
            except Exception:
                pass  # Redis 故障不影响主流程

        return self._to_dto(prefs)

    @staticmethod
    def _to_dto(prefs):
        """从当前身份空间构造独立偏好 DTO。"""
        if not uses_canonical_storage():
            return UserSearchPreferencesDTO.model_validate(prefs)
        return UserSearchPreferencesDTO.model_validate(
            {
                **prefs.model_dump(),
                "user_id": prefs.fluxer_user_id,
                "include_authors": prefs.fluxer_include_authors,
                "exclude_authors": prefs.fluxer_exclude_authors,
            }
        )
