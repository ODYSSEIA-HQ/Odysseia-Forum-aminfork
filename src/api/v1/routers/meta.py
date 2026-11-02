import hashlib
import logging
from typing import Dict, List, Optional, Union

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response

from api.v1.dependencies.security import get_current_user, require_auth
from core.cache_service import CacheService
from core.user_role_service import UserRoleService
from identity.runtime import is_fluxer_mode
from dto.meta import ChannelDetail
from dto.meta.user_role import UserRole
from meta.meta_service import MetaService
from shared.database import AsyncSessionFactory
from shared.enum import ConstantEnum, SearchConfigType
from shared.redis_client import RedisManager
from shared.tag_error import TagError

logger = logging.getLogger(__name__)

# 全局依赖，将在 bot_main.py 中被注入
cache_service_instance: Optional[CacheService] = None
channel_mappings_config: Dict[int, List[Dict]] = {}
role_config: dict | None = None


router = APIRouter(
    prefix="/meta", tags=["元数据"], dependencies=[Depends(get_current_user)]
)


@router.get("/role", response_model=UserRole, summary="查询当前用户的管理身份")
async def get_user_role(
    response: Response,
    current_user: dict = Depends(require_auth),
) -> UserRole:
    """返回是否为主服务器管理组和 BOT 管理员，用于前端控制按钮显示。"""
    cache_headers = {"Cache-Control": "private, no-store"}
    response.headers.update(cache_headers)
    if role_config is None:
        raise HTTPException(503, "用户身份查询服务尚未初始化", headers=cache_headers)
    try:
        # 查询并判定当前用户是否为管理组身份和 BOT 管理员身份。
        discord_id = (
            current_user.get("discord_user_id")
            if is_fluxer_mode()
            else current_user["id"]
        )
        if not discord_id:
            return UserRole(is_management_member=False, is_bot_admin=False)
        return await UserRoleService(role_config).get_role(int(discord_id))
    except TagError as exc:
        raise HTTPException(exc.status, exc.detail, headers=cache_headers) from exc


def _build_channel_meta_cache_key(
    guild_id: Optional[int], channel_ids: Optional[list[int]], metadata_version: str
) -> str:
    """将频道快照和映射配置版本加入缓存键，隔离不同 API 实例的旧目录。"""
    gid = str(guild_id) if guild_id else "all"
    cids = "-".join(str(c) for c in sorted(channel_ids)) if channel_ids else "all"
    # 配置变化后的重启也不能复用旧虚拟频道映射。
    mapping_version = hashlib.sha256(
        orjson.dumps(
            channel_mappings_config,
            option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS,
        )
    ).hexdigest()[:16]
    return f"cache:meta:channels:v2:{metadata_version}:{mapping_version}:{gid}:{cids}"


@router.get(
    "/channels", response_model=List[ChannelDetail], summary="获取频道目录与基础信息"
)
async def get_indexed_channels_with_tags(
    channel_ids: Optional[List[Union[int, str]]] = Query(
        default=None, description="要查询的特定频道ID列表"
    ),
    guild_id: Optional[Union[int, str]] = Query(
        default=None, description="按服务器ID过滤频道"
    ),
):
    """返回指定频道的标签、虚拟映射及发帖统计量"""
    if not cache_service_instance:
        raise HTTPException(status_code=503, detail="Cache 服务尚未初始化")

    #  ID 转换
    effective_guild_id = None
    if guild_id:
        try:
            effective_guild_id = int(guild_id)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"无效的服务器ID格式: {guild_id}"
            )

    effective_channel_ids = None
    if channel_ids:
        effective_channel_ids = []
        for cid in channel_ids:
            try:
                effective_channel_ids.append(int(cid))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"无效的频道ID格式: {cid}")

    metadata_version = getattr(
        cache_service_instance, "metadata_version", "unversioned"
    )
    cache_key = _build_channel_meta_cache_key(
        effective_guild_id, effective_channel_ids, metadata_version
    )

    # 尝试从 Redis 读取缓存
    try:
        redis = RedisManager.get_client()
        cached = await redis.get(cache_key)
        if cached is not None and metadata_version == getattr(
            cache_service_instance, "metadata_version", "unversioned"
        ):
            return Response(content=cached, media_type="application/json")
    except Exception:
        logger.warning("读取 Redis 频道元数据缓存失败，回退到数据库查询", exc_info=True)

    try:
        async with AsyncSessionFactory() as session:
            meta_service = MetaService(
                session=session,
                cache_service=cache_service_instance,
                channel_mappings=channel_mappings_config,
            )
            result = await meta_service.get_channels_meta(
                effective_guild_id, effective_channel_ids
            )

        # 写入 Redis 缓存
        try:
            serialized = orjson.dumps(
                [item.model_dump(mode="json", by_alias=True) for item in result]
            )
            # 查询过程中缓存发生刷新时，不把新目录写到旧版本的键下。
            if metadata_version == getattr(
                cache_service_instance, "metadata_version", "unversioned"
            ):
                await redis.setex(
                    cache_key,
                    ConstantEnum.CHANNELS_CACHE_EXPIRE_SECONDS.value,
                    serialized,
                )
        except Exception:
            logger.warning("写入 Redis 频道元数据缓存失败", exc_info=True)

        return Response(content=serialized, media_type="application/json")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取频道目录时发生内部错误: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取频道目录时发生内部错误",
        )


@router.get("/main-guild", summary="获取当前主服务器ID")
async def get_main_guild_id():
    """返回配置文件中定义的主服务器 ID (Main Guild ID)"""
    if not cache_service_instance:
        raise HTTPException(status_code=503, detail="Cache 服务尚未初始化")

    try:
        # 从缓存中获取主服务器配置
        config = await cache_service_instance.get_bot_config(
            SearchConfigType.MAIN_GUILD_ID
        )

        if not config or config.value_int is None:
            # 如果数据库中没找到，理论上不应该发生，因为 bot_main 会初始化它
            return {"main_guild_id": "0"}

        # 将 ID 转换为字符串返回，防止前端 JavaScript 丢失大整数精度
        return {"main_guild_id": str(config.value_int)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取主服务器ID时发生内部错误: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取主服务器ID时发生内部错误",
        )
