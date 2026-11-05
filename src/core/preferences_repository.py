from identity.business_identity import discord_id_for_business_id
import json
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
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
        self, user_id: int | str, guild_id: int = 0
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
        prefs_orm = result.scalars().one_or_none()
        if not prefs_orm:
            return None
        return await self._read_dto(prefs_orm)

    async def _read_dto(self, prefs):
        """兼容旧作者映射但不在读取时修改 ORM，避免读后提交覆盖新偏好。"""
        if not uses_canonical_storage():
            return self._to_dto(prefs)
        values = prefs.model_dump()
        for field in ("include_authors", "exclude_authors"):
            canonical = getattr(prefs, "fluxer_" + field)
            if canonical is None and getattr(prefs, field) is not None:
                canonical = await resolve_business_ids(
                    self.session, getattr(prefs, field), source="discord"
                )
            values[field] = canonical
        values["user_id"] = prefs.fluxer_user_id
        return UserSearchPreferencesDTO.model_validate(values)

    async def lock_user_preferences(self, user_id, guild_id=0):
        """同一账户与服务器串行写入，首次尚无行时也覆盖所有 worker。"""
        user_id = await resolve_business_id(self.session, user_id)
        if self.session.get_bind().dialect.name == "postgresql":
            namespace = "fluxer" if uses_canonical_storage() else "discord"
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"forum:preferences:{namespace}:{user_id}:{guild_id}"},
            )
        return user_id

    async def save_user_preferences(
        self, user_id: int, prefs_data: dict, guild_id: int = 0
    ) -> UserSearchPreferencesDTO:
        """创建或更新用户在指定服务器的搜索偏好设置"""
        user_id = await self.lock_user_preferences(user_id, guild_id)
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
        prefs = result.scalars().one_or_none()
        if uses_canonical_storage():
            prefs_data = dict(prefs_data)
            for field in ("include_authors", "exclude_authors"):
                if field in prefs_data:
                    value = prefs_data.pop(field)
                    prefs_data["fluxer_" + field] = (
                        await resolve_business_ids(self.session, value)
                        if value is not None
                        else []
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

        result_dto = await self._read_dto(prefs)

        # 写入成功后刷新 Redis 缓存
        if self._redis:
            try:
                cache_key = (
                    "fluxer:" if uses_canonical_storage() else ""
                ) + CacheKeys.USER_PREFERENCES.format(
                    user_id=user_id, guild_id=guild_id
                )
                await self._redis.setex(
                    cache_key,
                    int(ConstantEnum.PREF_CACHE_TTL),
                    json.dumps(result_dto.model_dump(), default=str),
                )
            except Exception:
                pass  # Redis 故障不影响主流程

        return result_dto

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
